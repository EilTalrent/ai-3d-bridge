# SPDX-License-Identifier: GPL-3.0-or-later
"""
Meshy AI Bridge for Blender (unofficial).

Generate 3D models from a text prompt or an image, retexture, remesh,
auto-rig and animate - all from Blender's sidebar, using the Meshy API.
You need your own Meshy API key: https://www.meshy.ai/settings/api
"""

bl_info = {
    "name": "AI 3D Bridge (Hunyuan3D local + Meshy)",
    "author": "Generated with Claude",
    "version": (1, 3, 0),
    "blender": (3, 3, 0),
    "location": "3D Viewport > Sidebar (N) > Meshy",
    "description": "Text/Image to 3D and AI texturing offline with Hunyuan3D, or via the Meshy API",
    "category": "3D View",
}

import base64
import json
import os
import re
import ssl
import tempfile
import threading
import time
import urllib.error
import urllib.request
import webbrowser

import bpy

# Works as an extension (4.2+), a zipped add-on folder, or a single .py file
ADDON_ID = __package__ or __name__

from bpy.props import (
    BoolProperty,
    EnumProperty,
    FloatProperty,
    IntProperty,
    PointerProperty,
    StringProperty,
)

API_BASE = "https://api.meshy.ai/openapi"
USER_AGENT = "MeshyBlenderBridge/1.0"
POLL_SECONDS = 3.0
TERMINAL = {"SUCCEEDED", "FAILED", "CANCELED"}

# Custom properties written onto imported objects so later steps can chain
# server-side (no re-upload needed).
PROP_TASK_ID = "meshy_task_id"
PROP_TASK_KIND = "meshy_task_kind"
PROP_RIG_ID = "meshy_rig_task_id"


# ---------------------------------------------------------------------------
# HTTP helpers (thread-safe: no bpy access in here)
# ---------------------------------------------------------------------------

class MeshyError(Exception):
    pass


def _ssl_context():
    try:
        import certifi  # bundled with most Blender builds
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


_SSL = None


def _ctx():
    global _SSL
    if _SSL is None:
        _SSL = _ssl_context()
    return _SSL


def _friendly_http_error(err):
    try:
        body = err.read().decode("utf-8", "replace")
        msg = json.loads(body).get("message", body)
    except Exception:
        msg = str(err)
    hints = {
        401: "Invalid or missing API key (set it in the add-on preferences).",
        402: "Not enough Meshy credits, or your plan doesn't include this feature.",
        404: "Not found (the feature may not be available to your account).",
        429: "Rate limited by Meshy - try again in a moment.",
    }
    hint = hints.get(err.code)
    return MeshyError(f"HTTP {err.code}: {msg}" + (f" - {hint}" if hint else ""))


def api_request(api_key, method, path, body=None, timeout=60):
    """Returns (parsed_json, headers)."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(API_BASE + path, data=data, method=method)
    req.add_header("Authorization", f"Bearer {api_key}")
    req.add_header("User-Agent", USER_AGENT)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ctx()) as resp:
            raw = resp.read()
            payload = json.loads(raw) if raw else {}
            return payload, resp.headers
    except urllib.error.HTTPError as e:
        raise _friendly_http_error(e) from None
    except urllib.error.URLError as e:
        raise MeshyError(f"Network error: {e.reason}") from None


def create_task(api_key, path, body):
    payload, _ = api_request(api_key, "POST", path, body)
    task_id = payload.get("result")
    if not task_id:
        raise MeshyError(f"Unexpected response creating task: {payload}")
    return task_id


def download_file(url, dest_path):
    # Asset URLs are pre-signed; don't send the API key to them.
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=300, context=_ctx()) as resp, \
                open(dest_path, "wb") as fh:
            while True:
                chunk = resp.read(1 << 16)
                if not chunk:
                    break
                fh.write(chunk)
    except urllib.error.HTTPError as e:
        raise _friendly_http_error(e) from None
    except urllib.error.URLError as e:
        raise MeshyError(f"Download failed: {e.reason}") from None
    return dest_path


def file_to_data_uri(path):
    ext = os.path.splitext(path)[1].lower()
    mime = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".glb": "model/gltf-binary",
    }.get(ext)
    if mime is None:
        raise MeshyError(f"Unsupported file type '{ext}'.")
    with open(path, "rb") as fh:
        encoded = base64.b64encode(fh.read()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


# ---------------------------------------------------------------------------
# Jobs: run in a background thread; a bpy timer reads their state and does
# the Blender-side work (import) on the main thread.
# ---------------------------------------------------------------------------

class Job:
    _counter = 0

    def __init__(self, label, kind):
        Job._counter += 1
        self.id = Job._counter
        self.label = label
        self.kind = kind            # text, image, retexture, remesh, rig, animate
        self.phase = "Starting"
        self.progress = 0
        self.queue_pos = 0
        self.done = False
        self.error = None
        self.canceled = False
        self.cancel_requested = False
        self.result_path = None
        self.result_task_id = None
        self.result_rig_id = None
        self.imported = False
        self.finished_at = None
        self.extra_files = []       # (label, path) for side downloads


JOBS = []
_LOCK = threading.Lock()


def _poll(job, api_key, path, phase, pmin, pmax):
    job.phase = phase
    while True:
        if job.cancel_requested:
            raise _Canceled()
        task, headers = api_request(api_key, "GET", path)
        status = task.get("status", "")
        pct = int(task.get("progress") or 0)
        job.progress = int(pmin + (pmax - pmin) * pct / 100.0)
        job.queue_pos = int(task.get("preceding_tasks") or 0)
        if status == "SUCCEEDED":
            return task
        if status in ("FAILED", "CANCELED"):
            err = (task.get("task_error") or {}).get("message") or status
            raise MeshyError(f"{phase} failed: {err}")
        wait = POLL_SECONDS
        try:
            ra = headers.get("Retry-After") if headers else None
            if ra:
                wait = max(1.0, min(10.0, float(ra)))
        except Exception:
            pass
        # sleep in small steps so cancel is responsive
        end = time.time() + wait
        while time.time() < end:
            if job.cancel_requested:
                raise _Canceled()
            time.sleep(0.2)


class _Canceled(Exception):
    pass


def _glb_from_task(task):
    urls = task.get("model_urls") or {}
    url = urls.get("glb") or task.get("model_url")
    if not url:
        raise MeshyError("Task finished but returned no GLB download.")
    return url


def _dest(out_dir, stem):
    os.makedirs(out_dir, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", stem).strip("_")[:60] or "meshy"
    return os.path.join(out_dir, f"{safe}.glb")


def pipeline_text(job, api_key, p, out_dir):
    preview = {
        "mode": "preview",
        "prompt": p["prompt"],
        "ai_model": p["ai_model"],
        "target_polycount": p["polycount"],
    }
    if p["ai_model"] == "meshy-t2.5":
        preview["model_type"] = "smart-topology"
    else:
        preview["topology"] = p["topology"]
    if p["negative_prompt"]:
        preview["negative_prompt"] = p["negative_prompt"]
    if p["pose_mode"] != "NONE":
        preview["pose_mode"] = p["pose_mode"]
    if p["seed"] > 0:
        preview["seed"] = p["seed"]

    texture = p["texture"]
    tid = create_task(api_key, "/v2/text-to-3d", preview)
    task = _poll(job, api_key, f"/v2/text-to-3d/{tid}", "Generating shape",
                 0, 50 if texture else 100)

    if texture:
        refine = {"mode": "refine", "preview_task_id": tid,
                  "enable_pbr": p["pbr"]}
        if p["texture_prompt"]:
            refine["texture_prompt"] = p["texture_prompt"]
        tid = create_task(api_key, "/v2/text-to-3d", refine)
        task = _poll(job, api_key, f"/v2/text-to-3d/{tid}", "Texturing", 50, 100)

    job.phase = "Downloading"
    job.result_path = download_file(_glb_from_task(task),
                                    _dest(out_dir, f"meshy_{p['prompt'][:40]}_{tid[:8]}"))
    job.result_task_id = tid


def pipeline_image(job, api_key, p, out_dir):
    body = {
        "image_url": file_to_data_uri(p["image_path"]),
        "ai_model": p["ai_model"],
        "should_texture": p["texture"],
        "enable_pbr": p["pbr"] and p["texture"],
        "target_polycount": p["polycount"],
    }
    if p["ai_model"] == "meshy-t2.5":
        body["model_type"] = "smart-topology"
    else:
        body["topology"] = p["topology"]
    if p["texture"] and p["texture_prompt"]:
        body["texture_prompt"] = p["texture_prompt"]
    if p["pose_mode"] != "NONE":
        body["pose_mode"] = p["pose_mode"]
    tid = create_task(api_key, "/v1/image-to-3d", body)
    task = _poll(job, api_key, f"/v1/image-to-3d/{tid}", "Generating model", 0, 100)
    job.phase = "Downloading"
    stem = os.path.splitext(os.path.basename(p["image_path"]))[0]
    job.result_path = download_file(_glb_from_task(task),
                                    _dest(out_dir, f"meshy_{stem}_{tid[:8]}"))
    job.result_task_id = tid


def pipeline_multi(job, api_key, p, out_dir):
    body = {
        "image_urls": [file_to_data_uri(v) for v in p["views"].values()],
        "ai_model": p["ai_model"] if p["ai_model"] in ("latest", "meshy-6", "meshy-6-lite") else "latest",
        "should_texture": p["texture"],
        "enable_pbr": p["pbr"] and p["texture"],
        "topology": p["topology"],
        "target_polycount": p["polycount"],
    }
    if p["texture"] and p["texture_prompt"]:
        body["texture_prompt"] = p["texture_prompt"]
    if p["pose_mode"] != "NONE":
        body["pose_mode"] = p["pose_mode"]
    tid = create_task(api_key, "/v1/multi-image-to-3d", body)
    task = _poll(job, api_key, f"/v1/multi-image-to-3d/{tid}", "Generating model", 0, 100)
    job.phase = "Downloading"
    job.result_path = download_file(_glb_from_task(task),
                                    _dest(out_dir, f"meshy_views_{tid[:8]}"))
    job.result_task_id = tid


def _source(p):
    """input_task_id when reusing, otherwise upload the exported GLB."""
    if p.get("input_task_id"):
        return {"input_task_id": p["input_task_id"]}
    return {"model_url": file_to_data_uri(p["glb_path"])}


def pipeline_retexture(job, api_key, p, out_dir):
    body = _source(p)
    if p["style_image"]:
        body["image_style_url"] = file_to_data_uri(p["style_image"])
    else:
        body["text_style_prompt"] = p["texture_prompt"]
    body["enable_pbr"] = p["pbr"]
    body["enable_original_uv"] = p["keep_uv"]
    tid = create_task(api_key, "/v1/retexture", body)
    task = _poll(job, api_key, f"/v1/retexture/{tid}", "Texturing", 0, 100)
    job.phase = "Downloading"
    job.result_path = download_file(_glb_from_task(task),
                                    _dest(out_dir, f"meshy_retex_{tid[:8]}"))
    job.result_task_id = tid


def pipeline_remesh(job, api_key, p, out_dir):
    body = _source(p)
    body.update({"target_formats": ["glb"], "topology": p["topology"],
                 "target_polycount": p["polycount"]})
    tid = create_task(api_key, "/v1/remesh", body)
    task = _poll(job, api_key, f"/v1/remesh/{tid}", "Remeshing", 0, 100)
    job.phase = "Downloading"
    job.result_path = download_file(_glb_from_task(task),
                                    _dest(out_dir, f"meshy_remesh_{tid[:8]}"))
    job.result_task_id = tid


def pipeline_rig(job, api_key, p, out_dir):
    body = _source(p)
    body.update({"animation_type": p["rig_type"], "height_meters": p["height"]})
    tid = create_task(api_key, "/v1/rigging", body)
    task = _poll(job, api_key, f"/v1/rigging/{tid}", "Rigging", 0, 100)
    result = task.get("result") or {}
    url = result.get("rigged_character_glb_url")
    if not url:
        raise MeshyError("Rigging finished but returned no GLB.")
    job.phase = "Downloading"
    job.result_path = download_file(url, _dest(out_dir, f"meshy_rigged_{tid[:8]}"))
    job.result_rig_id = tid
    job.result_task_id = tid
    anims = result.get("basic_animations") or {}
    if p.get("with_basic_anims"):
        for key, label in (("walking_glb_url", "Walk"), ("running_glb_url", "Run")):
            if anims.get(key):
                path = download_file(anims[key],
                                     _dest(out_dir, f"meshy_{label.lower()}_{tid[:8]}"))
                job.extra_files.append((label, path))


def pipeline_animate(job, api_key, p, out_dir):
    body = {"rig_task_id": p["rig_task_id"], "action_id": p["action_id"]}
    tid = create_task(api_key, "/v1/animations", body)
    task = _poll(job, api_key, f"/v1/animations/{tid}", "Animating", 0, 100)
    url = (task.get("result") or {}).get("animation_glb_url")
    if not url:
        raise MeshyError("Animation finished but returned no GLB.")
    job.phase = "Downloading"
    job.result_path = download_file(url, _dest(out_dir, f"meshy_anim{p['action_id']}_{tid[:8]}"))
    job.result_task_id = tid
    job.result_rig_id = p["rig_task_id"]


# ---------------------------------------------------------------------------
# Local backend: Hunyuan3D server on this computer (hunyuan_server.py)
# ---------------------------------------------------------------------------

def local_request(base, method, path, body=None, timeout=30):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(base.rstrip("/") + path, data=data, method=method)
    req.add_header("User-Agent", USER_AGENT)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read().decode("utf-8", "replace")).get("detail", "")
        except Exception:
            msg = ""
        raise MeshyError(f"Local server error {e.code}: {msg or e.reason}") from None
    except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
        reason = getattr(e, "reason", e)
        raise MeshyError(f"Can't reach the local server at {base} ({reason}). "
                         "Is start_hunyuan.sh running and showing 'server ready'?") from None


def _b64_file(path):
    with open(path, "rb") as fh:
        return base64.b64encode(fh.read()).decode("ascii")


def pipeline_local(job, base, p, out_dir):
    body = {
        "mode": p["mode"],
        "texture": p["texture"],
        "seed": p["seed"] if p["seed"] > 0 else int(time.time()) % 1000000,
        "octree_resolution": p["octree"],
        "face_count": p["face_count"],
    }
    if p.get("prompt"):
        body["prompt"] = p["prompt"]
    if p.get("image_path"):
        body["image"] = _b64_file(p["image_path"])
    if p.get("glb_path"):
        body["mesh"] = _b64_file(p["glb_path"])
    if p.get("views"):
        body["views"] = {k: _b64_file(v) for k, v in p["views"].items()}

    jid = local_request(base, "POST", "/jobs", body, timeout=300)["id"]
    job.phase = "Queued"
    stalled_since = None
    while True:
        if job.cancel_requested:
            try:
                local_request(base, "POST", f"/jobs/{jid}/cancel", timeout=5)
            except MeshyError:
                pass
            raise _Canceled()
        try:
            st = local_request(base, "GET", f"/jobs/{jid}", timeout=20)
            stalled_since = None
        except MeshyError:
            # The server can stall briefly while it shuffles models between
            # RAM and GPU; only give up after two minutes of silence.
            stalled_since = stalled_since or time.time()
            if time.time() - stalled_since > 120:
                raise
            time.sleep(3.0)
            continue
        job.phase = st.get("phase") or job.phase
        job.progress = int(st.get("progress") or 0)
        job.queue_pos = int(st.get("queue_position") or 0)
        if st["status"] == "done":
            break
        if st["status"] in ("failed", "canceled"):
            raise MeshyError(st.get("error") or "Job " + st["status"])
        time.sleep(1.0)

    job.phase = "Downloading"
    first_view = next(iter((p.get("views") or {}).values()), None)
    stem = p.get("prompt") or os.path.splitext(
        os.path.basename(p.get("image_path") or first_view or "model"))[0]
    dest = _dest(out_dir, f"hunyuan_{stem[:40]}_{jid[:6]}")
    req = urllib.request.Request(base.rstrip("/") + f"/jobs/{jid}/result",
                                 headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp, open(dest, "wb") as fh:
            fh.write(resp.read())
    except (urllib.error.URLError, ConnectionError) as e:
        raise MeshyError(f"Download from local server failed: {e}") from None
    job.result_path = dest


PIPELINES = {
    "local": pipeline_local,
    "text": pipeline_text,
    "image": pipeline_image,
    "multi": pipeline_multi,
    "retexture": pipeline_retexture,
    "remesh": pipeline_remesh,
    "rig": pipeline_rig,
    "animate": pipeline_animate,
}


def _run_job(job, api_key, params, out_dir):
    try:
        PIPELINES[job.kind](job, api_key, params, out_dir)
        job.phase = "Importing"
        job.progress = 100
    except _Canceled:
        job.canceled = True
        job.phase = "Canceled"
    except MeshyError as e:
        job.error = str(e)
        job.phase = "Failed"
    except Exception as e:  # never let a thread die silently
        job.error = f"{type(e).__name__}: {e}"
        job.phase = "Failed"
    finally:
        tmp = params.get("glb_path")
        if tmp and params.get("glb_is_temp"):
            try:
                os.remove(tmp)
            except OSError:
                pass
        job.done = True


def start_job(label, kind, api_key, params, out_dir):
    job = Job(label, kind)
    with _LOCK:
        JOBS.insert(0, job)
        del JOBS[12:]
    threading.Thread(target=_run_job, args=(job, api_key, params, out_dir),
                     daemon=True).start()
    if not bpy.app.timers.is_registered(_tick):
        bpy.app.timers.register(_tick, first_interval=0.5)
    return job


# ---------------------------------------------------------------------------
# Main-thread side: timer, import, redraw
# ---------------------------------------------------------------------------

def _redraw():
    wm = getattr(bpy.context, "window_manager", None)
    if not wm:
        return
    for win in wm.windows:
        for area in win.screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()


def _view3d_override():
    wm = bpy.context.window_manager
    for win in wm.windows:
        for area in win.screen.areas:
            if area.type == "VIEW_3D":
                region = next((r for r in area.regions if r.type == "WINDOW"), None)
                return {"window": win, "screen": win.screen, "area": area, "region": region}
    return None


def import_glb(path):
    before = set(bpy.data.objects)
    ov = _view3d_override()
    if ov and hasattr(bpy.context, "temp_override"):
        with bpy.context.temp_override(**ov):
            bpy.ops.import_scene.gltf(filepath=path)
    else:
        bpy.ops.import_scene.gltf(filepath=path)
    return [o for o in bpy.data.objects if o not in before]


def _finish_import(job):
    new_objs = import_glb(job.result_path)
    roots = [o for o in new_objs if o.parent is None or o.parent not in new_objs]
    cursor = bpy.context.scene.cursor.location.copy()
    base = re.sub(r"\s+", "_", job.label)[:40]
    for i, o in enumerate(roots):
        o.location = cursor + o.location
        o.name = f"Meshy_{base}" if len(roots) == 1 else f"Meshy_{base}_{i + 1}"
    for o in new_objs:
        if job.result_task_id:
            o[PROP_TASK_ID] = job.result_task_id
            o[PROP_TASK_KIND] = job.kind
        if job.result_rig_id:
            o[PROP_RIG_ID] = job.result_rig_id
    for label, path in job.extra_files:
        extra = import_glb(path)
        for o in extra:
            if o.parent is None or o.parent not in extra:
                o.location = cursor + o.location
                o.name = f"Meshy_{base}_{label}"
            if job.result_rig_id:
                o[PROP_RIG_ID] = job.result_rig_id
    # select the result
    for o in bpy.context.view_layer.objects:
        o.select_set(False)
    for o in new_objs:
        try:
            o.select_set(True)
        except RuntimeError:
            pass
    if roots:
        bpy.context.view_layer.objects.active = roots[0]


def _tick():
    pending = False
    for job in list(JOBS):
        if job.done and not job.imported:
            job.imported = True
            job.finished_at = time.time()
            if job.error is None and not job.canceled and job.result_path:
                try:
                    _finish_import(job)
                    job.phase = "Done"
                except Exception as e:
                    job.error = f"Import failed: {e}"
                    job.phase = "Failed"
        if not job.done:
            pending = True
    _redraw()
    return 0.5 if pending else None


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------

def _prefs(context):
    addon = context.preferences.addons.get(ADDON_ID)
    return addon.preferences if addon else None


def _api_key(context):
    prefs = _prefs(context)
    key = (prefs.api_key if prefs else "").strip()
    return key or os.environ.get("MESHY_API_KEY", "").strip()


def _backend(context):
    prefs = _prefs(context)
    return prefs.backend if prefs else "LOCAL"


def _local_url(context):
    prefs = _prefs(context)
    return (prefs.local_url if prefs else "").strip() or "http://127.0.0.1:8081"


def _out_dir(context):
    prefs = _prefs(context)
    d = bpy.path.abspath(prefs.download_dir) if prefs and prefs.download_dir else ""
    if not d:
        if bpy.data.filepath:
            d = os.path.join(os.path.dirname(bpy.data.filepath), "meshy_models")
        else:
            d = os.path.join(tempfile.gettempdir(), "meshy_models")
    return d


MODEL_ITEMS = [
    ("latest", "Latest", "Meshy's newest model"),
    ("meshy-6", "Meshy 6", ""),
    ("meshy-6-lite", "Meshy 6 Lite", "Faster, cheaper"),
    ("meshy-t2.5", "Smart Topology (T2.5)", "Clean low-poly topology (triangles only)"),
]

POSE_ITEMS = [
    ("NONE", "As described", ""),
    ("a-pose", "A-pose", "Force an A-pose (good for rigging characters)"),
    ("t-pose", "T-pose", "Force a T-pose"),
]


class MESHY_Preferences(bpy.types.AddonPreferences):
    bl_idname = ADDON_ID

    backend: EnumProperty(
        name="Backend",
        items=[
            ("LOCAL", "Local (Hunyuan3D)", "Free and offline, runs on your own NVIDIA GPU", "DESKTOP", 0),
            ("MESHY", "Meshy cloud", "Meshy's servers; needs a paid API key", "WORLD", 1),
        ],
        default="LOCAL",
    )
    local_url: StringProperty(
        name="Local server", default="http://127.0.0.1:8081",
        description="Address printed by start_hunyuan.sh")
    api_key: StringProperty(
        name="API Key", subtype="PASSWORD",
        description="Your Meshy API key (msy_...). The MESHY_API_KEY environment variable also works")
    download_dir: StringProperty(
        name="Download folder", subtype="DIR_PATH",
        description="Where downloaded models are saved. Empty = 'meshy_models' next to the .blend (or temp)")

    def draw(self, context):
        col = self.layout.column()
        col.row().prop(self, "backend", expand=True)
        if self.backend == "LOCAL":
            row = col.row(align=True)
            row.prop(self, "local_url")
            row.operator("meshy.check_server", text="", icon="FILE_REFRESH")
        else:
            col.prop(self, "api_key")
            row = col.row(align=True)
            row.operator("meshy.open_url", text="Get an API key", icon="URL").url = \
                "https://www.meshy.ai/settings/api"
            row.operator("meshy.check_balance", icon="FUND")
        col.prop(self, "download_dir")


class MESHY_Props(bpy.types.PropertyGroup):
    mode: EnumProperty(
        name="Mode",
        items=[
            ("TEXT", "Text", "Text to 3D", "FONT_DATA", 0),
            ("IMAGE", "Image", "Image to 3D", "IMAGE_DATA", 1),
            ("MULTI", "Views", "Build from several views of the same object "
                               "(front, back, sides) - best for character sheets", "IMAGE_REFERENCE", 5),
            ("TEXTURE", "Texture", "AI-texture the selected model", "TEXTURE", 2),
            ("REMESH", "Remesh", "Retopologize the selected model", "MOD_REMESH", 3),
            ("RIG", "Rig", "Auto-rig and animate the selected character", "ARMATURE_DATA", 4),
        ],
        default="TEXT",
    )
    prompt: StringProperty(name="Prompt", description="Describe the object (max 800 chars)",
                           maxlen=800)
    negative_prompt: StringProperty(name="Avoid", description="What to avoid", maxlen=800)
    image_path: StringProperty(name="Image", subtype="FILE_PATH",
                               description="PNG, JPG or WEBP of the object")
    mv_front: StringProperty(name="Front", subtype="FILE_PATH",
                             description="Object seen from the front")
    mv_back: StringProperty(name="Back", subtype="FILE_PATH",
                            description="Object seen from behind")
    mv_left: StringProperty(name="Left", subtype="FILE_PATH",
                            description="Side view with the object facing the LEFT edge of the picture")
    mv_right: StringProperty(name="Right", subtype="FILE_PATH",
                             description="Side view with the object facing the RIGHT edge of the picture")
    ai_model: EnumProperty(name="Model", items=MODEL_ITEMS, default="latest")
    topology: EnumProperty(name="Topology",
                           items=[("triangle", "Triangles", ""), ("quad", "Quads", "")],
                           default="triangle")
    polycount: IntProperty(name="Target polys", default=30000, min=100, max=300000,
                           soft_max=100000)
    pose_mode: EnumProperty(name="Pose", items=POSE_ITEMS, default="NONE")
    seed: IntProperty(name="Seed", default=0, min=0, description="0 = random")
    texture: BoolProperty(name="Generate texture", default=True,
                          description="Run the texturing stage (uses extra credits)")
    pbr: BoolProperty(name="PBR maps", default=True,
                      description="Metallic, roughness and normal maps too")
    texture_prompt: StringProperty(name="Texture prompt", maxlen=800,
                                   description="Optional description of materials and colors")
    style_image: StringProperty(name="Style image", subtype="FILE_PATH",
                                description="Optional reference image instead of a text prompt")
    keep_uv: BoolProperty(name="Keep original UVs", default=False)
    reuse_task: BoolProperty(
        name="Reuse Meshy task", default=True,
        description="If the selection came from Meshy, reference it server-side instead of "
                    "uploading. Turn off if you edited the mesh in Blender")
    rig_type: EnumProperty(name="Skeleton",
                           items=[("biped", "Biped", "Humanoid"),
                                  ("quadruped", "Quadruped", "Four-legged")],
                           default="biped")
    height: FloatProperty(name="Height (m)", default=1.7, min=0.05, soft_max=10.0)
    with_basic_anims: BoolProperty(name="Also import walk & run", default=False)
    action_id: IntProperty(name="Action ID", default=0, min=0, max=32767,
                           description="Animation ID from Meshy's animation library")
    show_advanced: BoolProperty(name="Advanced", default=False)
    local_detail: EnumProperty(
        name="Detail",
        items=[("196", "Draft", "Fastest, softer shapes"),
               ("256", "Standard", "Good balance"),
               ("384", "High", "Sharper detail, slower, more memory")],
        default="256")
    retopo_faces: IntProperty(
        name="Target faces", default=15000, min=1000, max=100000, soft_max=40000,
        description="Triangle count after decimation. QRemeshify is happiest under ~20k")
    retopo_min_island: FloatProperty(
        name="Remove bits under", default=0.5, min=0.0, max=20.0, subtype="PERCENTAGE",
        description="Delete separate pieces smaller than this share of the whole mesh "
                    "(AI floaters). 0 keeps everything")
    retopo_fill_holes: BoolProperty(name="Fill holes", default=True)
    retopo_center: BoolProperty(
        name="Center for symmetry", default=True,
        description="Put the object origin at the middle of the mesh so symmetry options line up")
    local_faces: IntProperty(
        name="Texture faces", default=40000, min=5000, max=150000, soft_max=80000,
        description="Mesh is reduced to this many faces before texturing. "
                    "Lower = faster and less GPU memory")


# ---------------------------------------------------------------------------
# Operators
# ---------------------------------------------------------------------------

def _check_ready(op, context):
    if hasattr(bpy.app, "online_access") and not bpy.app.online_access:
        op.report({"ERROR"}, "Online access is disabled. Enable it in "
                             "Preferences > System > Network.")
        return None
    key = _api_key(context)
    if not key:
        op.report({"ERROR"}, "Add your Meshy API key in the add-on preferences first.")
        return None
    return key


def _find_prop(obj, name):
    while obj is not None:
        if name in obj.keys():
            return obj[name]
        obj = obj.parent
    return None


def _selected_source(op, context, props, kind, allow_reuse=True):
    """Builds the source part of params from the selection."""
    obj = context.active_object
    sel = [o for o in context.selected_objects]
    if obj is None and not sel:
        op.report({"ERROR"}, "Select the model first.")
        return None
    if allow_reuse and props.reuse_task and obj is not None:
        tid = _find_prop(obj, PROP_TASK_ID)
        if tid:
            return {"input_task_id": tid}
    if not any(o.type == "MESH" for o in sel) and not (obj and obj.type == "MESH"):
        op.report({"ERROR"}, "Select at least one mesh object.")
        return None
    fd, path = tempfile.mkstemp(suffix=".glb", prefix="meshy_upload_")
    os.close(fd)
    try:
        bpy.ops.export_scene.gltf(filepath=path, export_format="GLB", use_selection=True)
    except Exception as e:
        op.report({"ERROR"}, f"GLB export failed: {e}")
        return None
    size_mb = os.path.getsize(path) / 1e6
    if size_mb > 80:
        op.report({"WARNING"}, f"Upload is {size_mb:.0f} MB - this may be slow or rejected.")
    return {"glb_path": path, "glb_is_temp": True}


def _collect_views(op, p):
    views = {}
    for key in ("front", "back", "left", "right"):
        raw = getattr(p, f"mv_{key}")
        if not raw:
            continue
        path = bpy.path.abspath(raw)
        if not os.path.isfile(path):
            op.report({"ERROR"}, f"{key.title()} image not found.")
            return None
        views[key] = path
    if not views:
        op.report({"ERROR"}, "Pick at least one view image (front is the most important).")
        return None
    return views


class MESHY_OT_generate(bpy.types.Operator):
    bl_idname = "meshy.generate"
    bl_label = "Generate"
    bl_description = "Send the job to Meshy; the result is imported when ready"

    def execute(self, context):
        if _backend(context) == "LOCAL":
            return self._local(context)
        key = _check_ready(self, context)
        if not key:
            return {"CANCELLED"}
        p = context.scene.meshy
        common = {
            "ai_model": p.ai_model, "topology": p.topology, "polycount": p.polycount,
            "pose_mode": p.pose_mode, "texture": p.texture, "pbr": p.pbr,
            "texture_prompt": p.texture_prompt.strip(), "seed": p.seed,
        }

        if p.mode == "TEXT":
            prompt = p.prompt.strip()
            if not prompt:
                self.report({"ERROR"}, "Type a prompt first.")
                return {"CANCELLED"}
            params = dict(common, prompt=prompt, negative_prompt=p.negative_prompt.strip())
            start_job(prompt, "text", key, params, _out_dir(context))

        elif p.mode == "IMAGE":
            path = bpy.path.abspath(p.image_path)
            if not path or not os.path.isfile(path):
                self.report({"ERROR"}, "Pick an image file first.")
                return {"CANCELLED"}
            params = dict(common, image_path=path)
            start_job(os.path.splitext(os.path.basename(path))[0], "image", key, params,
                      _out_dir(context))

        elif p.mode == "MULTI":
            views = _collect_views(self, p)
            if views is None:
                return {"CANCELLED"}
            label = os.path.splitext(os.path.basename(next(iter(views.values()))))[0]
            start_job(label, "multi", key, dict(common, views=views), _out_dir(context))

        elif p.mode == "TEXTURE":
            style = bpy.path.abspath(p.style_image) if p.style_image else ""
            if style and not os.path.isfile(style):
                self.report({"ERROR"}, "Style image not found.")
                return {"CANCELLED"}
            if not style and not p.texture_prompt.strip():
                self.report({"ERROR"}, "Give a texture prompt or a style image.")
                return {"CANCELLED"}
            src = _selected_source(self, context, p, "retexture")
            if src is None:
                return {"CANCELLED"}
            params = dict(common, style_image=style, keep_uv=p.keep_uv, **src)
            label = p.texture_prompt.strip() or "retexture"
            start_job(label, "retexture", key, params, _out_dir(context))

        elif p.mode == "REMESH":
            src = _selected_source(self, context, p, "remesh")
            if src is None:
                return {"CANCELLED"}
            params = dict(common, **src)
            start_job(f"remesh_{p.polycount}", "remesh", key, params, _out_dir(context))

        elif p.mode == "RIG":
            src = _selected_source(self, context, p, "rig")
            if src is None:
                return {"CANCELLED"}
            params = dict(common, rig_type=p.rig_type, height=p.height,
                          with_basic_anims=p.with_basic_anims, **src)
            start_job("rigged", "rig", key, params, _out_dir(context))

        self.report({"INFO"}, "Sent to Meshy - you can keep working.")
        return {"FINISHED"}

    def _local(self, context):
        p = context.scene.meshy
        params = {"texture": p.texture, "seed": p.seed, "octree": int(p.local_detail),
                  "face_count": p.local_faces}
        prompt = p.prompt.strip()
        tex_prompt = p.texture_prompt.strip()

        if p.mode == "TEXT":
            if not prompt:
                self.report({"ERROR"}, "Type a prompt first.")
                return {"CANCELLED"}
            params.update(mode="text", prompt=prompt)
            label = prompt
        elif p.mode == "IMAGE":
            path = bpy.path.abspath(p.image_path)
            if not path or not os.path.isfile(path):
                self.report({"ERROR"}, "Pick an image file first.")
                return {"CANCELLED"}
            params.update(mode="image", image_path=path)
            label = os.path.splitext(os.path.basename(path))[0]
        elif p.mode == "MULTI":
            views = _collect_views(self, p)
            if views is None:
                return {"CANCELLED"}
            params.update(mode="multiview", views=views)
            label = os.path.splitext(os.path.basename(next(iter(views.values()))))[0]
        elif p.mode == "TEXTURE":
            style = bpy.path.abspath(p.style_image) if p.style_image else ""
            if style and not os.path.isfile(style):
                self.report({"ERROR"}, "Style image not found.")
                return {"CANCELLED"}
            if not style and not tex_prompt:
                self.report({"ERROR"}, "Give a texture prompt or a reference image.")
                return {"CANCELLED"}
            src = _selected_source(self, context, p, "texture", allow_reuse=False)
            if src is None:
                return {"CANCELLED"}
            params.update(mode="texture", texture=True, **src)
            if style:
                params["image_path"] = style
            else:
                params["prompt"] = tex_prompt
            label = (tex_prompt or os.path.basename(style)) + "_textured"
        else:
            self.report({"ERROR"}, "This mode needs the Meshy cloud backend.")
            return {"CANCELLED"}

        start_job(label, "local", _local_url(context), params, _out_dir(context))
        self.report({"INFO"}, "Sent to your local Hunyuan3D server - you can keep working.")
        return {"FINISHED"}


class MESHY_OT_check_server(bpy.types.Operator):
    bl_idname = "meshy.check_server"
    bl_label = "Check local server"
    bl_description = "See whether the local Hunyuan3D server is running"

    def execute(self, context):
        try:
            info = local_request(_local_url(context), "GET", "/health", timeout=5)
        except MeshyError as e:
            SERVER_INFO.clear()
            SERVER_INFO["error"] = str(e)
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}
        SERVER_INFO.clear()
        SERVER_INFO.update(info)
        bits = [info.get("shape_model", "?").split("/")[-1],
                "texture on" if info.get("texture") else "texture OFF",
                "text on" if info.get("text_to_3d") else "text OFF"]
        self.report({"INFO"}, "Local server OK: " + ", ".join(bits))
        return {"FINISHED"}


SERVER_INFO = {}


class MESHY_OT_animate(bpy.types.Operator):
    bl_idname = "meshy.animate"
    bl_label = "Apply Animation"
    bl_description = "Apply a library animation to the selected Meshy-rigged character"

    def execute(self, context):
        key = _check_ready(self, context)
        if not key:
            return {"CANCELLED"}
        rig_id = _find_prop(context.active_object, PROP_RIG_ID)
        if not rig_id:
            self.report({"ERROR"}, "Select a character rigged by Meshy first (use Auto-Rig).")
            return {"CANCELLED"}
        p = context.scene.meshy
        start_job(f"anim_{p.action_id}", "animate", key,
                  {"rig_task_id": rig_id, "action_id": p.action_id}, _out_dir(context))
        self.report({"INFO"}, "Animation job sent to Meshy.")
        return {"FINISHED"}


class MESHY_OT_cancel(bpy.types.Operator):
    bl_idname = "meshy.cancel"
    bl_label = "Stop"
    bl_description = "Stop waiting for this job (Meshy may still finish and charge it)"
    job_id: IntProperty()

    def execute(self, context):
        for job in JOBS:
            if job.id == self.job_id:
                job.cancel_requested = True
        return {"FINISHED"}


class MESHY_OT_clear(bpy.types.Operator):
    bl_idname = "meshy.clear_jobs"
    bl_label = "Clear finished"

    def execute(self, context):
        with _LOCK:
            JOBS[:] = [j for j in JOBS if not j.done]
        return {"FINISHED"}


class MESHY_OT_balance(bpy.types.Operator):
    bl_idname = "meshy.check_balance"
    bl_label = "Check credits"
    bl_description = "Show your remaining Meshy credits"

    def execute(self, context):
        key = _check_ready(self, context)
        if not key:
            return {"CANCELLED"}
        try:
            data, _ = api_request(key, "GET", "/v1/balance", timeout=20)
        except MeshyError as e:
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}
        self.report({"INFO"}, f"Meshy credits: {data.get('balance', '?')}")
        return {"FINISHED"}


class MESHY_OT_open_url(bpy.types.Operator):
    bl_idname = "meshy.open_url"
    bl_label = "Open"
    url: StringProperty()

    def execute(self, context):
        webbrowser.open(self.url)
        return {"FINISHED"}


class MESHY_OT_reveal(bpy.types.Operator):
    bl_idname = "meshy.reveal"
    bl_label = "Open download folder"

    def execute(self, context):
        d = _out_dir(context)
        os.makedirs(d, exist_ok=True)
        bpy.ops.wm.path_open(filepath=d)
        return {"FINISHED"}


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Retopology prep: clean an AI mesh so QRemeshify / QuadriFlow don't choke
# ---------------------------------------------------------------------------

def _islands(bm):
    """Connected groups of faces (lists of BMFace)."""
    seen, groups = set(), []
    for f in bm.faces:
        if f.index in seen:
            continue
        stack, group = [f], []
        seen.add(f.index)
        while stack:
            cur = stack.pop()
            group.append(cur)
            for e in cur.edges:
                for nb in e.link_faces:
                    if nb.index not in seen:
                        seen.add(nb.index)
                        stack.append(nb)
        groups.append(group)
    return groups


def _clean_mesh(me, props):
    """Bmesh cleanup on a mesh datablock. Returns a dict of what happened."""
    import bmesh
    bm = bmesh.new()
    bm.from_mesh(me)
    info = {"faces_before": len(bm.faces)}

    size = max((v.co.length for v in bm.verts), default=1.0) or 1.0
    eps = size * 1e-5

    bmesh.ops.remove_doubles(bm, verts=list(bm.verts), dist=eps)
    bmesh.ops.dissolve_degenerate(bm, edges=list(bm.edges), dist=eps)

    loose_e = [e for e in bm.edges if not e.link_faces]
    if loose_e:
        bmesh.ops.delete(bm, geom=loose_e, context="EDGES")
    loose_v = [v for v in bm.verts if not v.link_faces]
    if loose_v:
        bmesh.ops.delete(bm, geom=loose_v, context="VERTS")

    removed = 0
    if props.retopo_min_island > 0 and bm.faces:
        bm.faces.ensure_lookup_table()
        bm.faces.index_update()
        groups = _islands(bm)
        total = len(bm.faces)
        biggest = max(len(g) for g in groups)
        small = [g for g in groups
                 if len(g) < total * props.retopo_min_island / 100.0 and len(g) != biggest]
        doomed = [f for g in small for f in g]
        if doomed:
            bmesh.ops.delete(bm, geom=doomed, context="FACES")
            removed = len(small)
        loose_v = [v for v in bm.verts if not v.link_faces]
        if loose_v:
            bmesh.ops.delete(bm, geom=loose_v, context="VERTS")
    info["islands_removed"] = removed

    filled = 0
    if props.retopo_fill_holes:
        boundary = [e for e in bm.edges if e.is_boundary]
        if boundary:
            res = bmesh.ops.holes_fill(bm, edges=boundary, sides=0)
            new_faces = res.get("faces", [])
            filled = len(new_faces)
            if new_faces:
                bmesh.ops.triangulate(bm, faces=new_faces)
    info["holes_filled"] = filled

    for e in bm.edges:  # sharp marks would be treated as hard constraints
        e.smooth = True
    for f in bm.faces:
        f.smooth = True
    bmesh.ops.recalc_face_normals(bm, faces=list(bm.faces))
    bm.to_mesh(me)
    bm.free()
    me.update()
    return info


def _clear_custom_normals(obj):
    try:
        with bpy.context.temp_override(object=obj, active_object=obj,
                                       selected_objects=[obj], selected_editable_objects=[obj]):
            bpy.ops.mesh.customdata_custom_splitnormals_clear()
    except Exception:
        pass  # older Blender / no custom normals


def _apply_decimate(context, obj, target):
    faces = len(obj.data.polygons)
    if faces <= target:
        return faces
    mod = obj.modifiers.new("RetopoPrepDecimate", "DECIMATE")
    mod.decimate_type = "COLLAPSE"
    mod.ratio = max(0.001, target / faces)
    mod.use_collapse_triangulate = True
    dg = context.evaluated_depsgraph_get()
    ev = obj.evaluated_get(dg)
    new_me = bpy.data.meshes.new_from_object(ev, preserve_all_data_layers=True, depsgraph=dg)
    obj.modifiers.remove(mod)
    old = obj.data
    new_me.name = old.name
    obj.data = new_me
    if old.users == 0:
        bpy.data.meshes.remove(old)
    return len(new_me.polygons)


class MESHY_OT_retopo_prep(bpy.types.Operator):
    bl_idname = "meshy.retopo_prep"
    bl_label = "Prep for Retopo"
    bl_description = ("Make a cleaned, decimated copy of the active mesh that QRemeshify / "
                      "QuadriFlow can handle. The original is kept and hidden")
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        o = context.active_object
        return o is not None and o.type == "MESH" and context.mode == "OBJECT"

    def execute(self, context):
        from mathutils import Matrix, Vector
        props = context.scene.meshy
        src = context.active_object
        if len(src.data.polygons) == 0:
            self.report({"ERROR"}, "This mesh has no faces.")
            return {"CANCELLED"}

        # copy, so the original (with its texture) stays untouched
        obj = src.copy()
        obj.data = src.data.copy()
        obj.name = f"{src.name}_retopo_ready"
        for col in src.users_collection:
            col.objects.link(obj)
        obj.modifiers.clear()
        obj.parent = None
        for key in (PROP_TASK_ID, PROP_TASK_KIND, PROP_RIG_ID):  # it's a new mesh now
            if key in obj.keys():
                del obj[key]
        obj.data.transform(src.matrix_world)       # apply all transforms
        obj.matrix_world = Matrix.Identity(4)

        info = _clean_mesh(obj.data, props)
        _clear_custom_normals(obj)
        final = _apply_decimate(context, obj, props.retopo_faces)

        if props.retopo_center and obj.data.vertices:
            xs = [v.co for v in obj.data.vertices]
            lo = Vector((min(c.x for c in xs), min(c.y for c in xs), min(c.z for c in xs)))
            hi = Vector((max(c.x for c in xs), max(c.y for c in xs), max(c.z for c in xs)))
            mid = (lo + hi) / 2
            obj.data.transform(Matrix.Translation(-mid))
            obj.location = mid

        src.hide_set(True)
        for o in context.view_layer.objects:
            o.select_set(False)
        obj.select_set(True)
        context.view_layer.objects.active = obj

        bits = [f"{info['faces_before']:,} -> {final:,} faces"]
        if info["islands_removed"]:
            bits.append(f"{info['islands_removed']} floating bits removed")
        if info["holes_filled"]:
            bits.append(f"{info['holes_filled']} hole faces filled")
        self.report({"INFO"}, "Ready for retopo: " + ", ".join(bits) + ". Save before remeshing!")
        return {"FINISHED"}


class MESHY_PT_retopo(bpy.types.Panel):
    bl_label = "Retopo Prep"
    bl_idname = "MESHY_PT_retopo"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "AI 3D"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        p = context.scene.meshy
        col = self.layout.column()
        obj = context.active_object
        if obj is not None and obj.type == "MESH":
            col.label(text=f"{obj.name}: {len(obj.data.polygons):,} faces", icon="MESH_DATA")
        else:
            col.label(text="Select a mesh", icon="INFO")
        col.prop(p, "retopo_faces")
        col.prop(p, "retopo_min_island")
        col.prop(p, "retopo_fill_holes")
        col.prop(p, "retopo_center")
        col.operator("meshy.retopo_prep", icon="MOD_DECIM")
        col.label(text="Then run QRemeshify on the copy.")


class MESHY_PT_main(bpy.types.Panel):
    bl_label = "AI 3D Generator"
    bl_idname = "MESHY_PT_main"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "AI 3D"

    def draw(self, context):
        if _backend(context) == "LOCAL":
            self._draw_local(context)
        else:
            self._draw_meshy(context)
        self._jobs(self.layout)

    def _draw_local(self, context):
        layout = self.layout
        p = context.scene.meshy

        box = layout.box()
        row = box.row(align=True)
        if SERVER_INFO.get("ok"):
            row.label(text="Local: " + SERVER_INFO.get("shape_model", "?").split("/")[-1],
                      icon="CHECKMARK")
        elif SERVER_INFO.get("error"):
            row.label(text="Local server not reachable", icon="ERROR")
        else:
            row.label(text="Local Hunyuan3D", icon="DESKTOP")
        row.operator("meshy.check_server", text="", icon="FILE_REFRESH")
        row.operator("preferences.addon_show", text="", icon="PREFERENCES").module = ADDON_ID

        row = layout.row(align=True)
        for m in ("TEXT", "IMAGE", "MULTI", "TEXTURE"):
            row.prop_enum(p, "mode", m)
        col = layout.column()

        if p.mode not in ("TEXT", "IMAGE", "MULTI", "TEXTURE"):
            col.label(text="Remesh / Rig need the Meshy cloud backend.", icon="INFO")
            col.label(text="Blender's Remesh modifier works offline.")
            return

        if p.mode == "TEXT":
            col.prop(p, "prompt", text="")
            if len(p.prompt) > 60:
                col.label(text="Only the first 60 characters are used", icon="INFO")
            col.prop(p, "texture")
            col.prop(p, "local_detail")
            if p.texture:
                col.prop(p, "local_faces")
            col.prop(p, "seed")
            col.operator("meshy.generate", text="Generate from Text", icon="PLAY")

        elif p.mode == "IMAGE":
            col.prop(p, "image_path", text="")
            col.prop(p, "texture")
            col.prop(p, "local_detail")
            if p.texture:
                col.prop(p, "local_faces")
            col.prop(p, "seed")
            col.operator("meshy.generate", text="Generate from Image", icon="PLAY")

        elif p.mode == "MULTI":
            if SERVER_INFO.get("ok") and not SERVER_INFO.get("multiview"):
                warn = col.box()
                warn.label(text="Server is in single-image mode", icon="ERROR")
                warn.label(text="Restart it with:  start_hunyuan.sh --multiview")
            self._views(col, p)
            col.prop(p, "texture")
            col.prop(p, "local_detail")
            if p.texture:
                col.prop(p, "local_faces")
            col.prop(p, "seed")
            col.operator("meshy.generate", text="Generate from Views", icon="PLAY")

        elif p.mode == "TEXTURE":
            obj = context.active_object
            hint = col.box()
            if obj is None:
                hint.label(text="Select a model", icon="INFO")
            else:
                hint.label(text=obj.name, icon="OBJECT_DATA")
                hint.label(text="New UVs are made; a textured copy is added")
            col.prop(p, "texture_prompt", text="Prompt")
            col.prop(p, "style_image", text="Or image")
            col.prop(p, "local_faces")
            col.prop(p, "seed")
            col.operator("meshy.generate", text="Texture Selected", icon="BRUSH_DATA")

    def _views(self, col, p):
        box = col.box()
        box.label(text="One figure per image, plain background", icon="INFO")
        for key in ("front", "back", "left", "right"):
            box.prop(p, f"mv_{key}")
        box.label(text="Left/Right = which edge the figure faces")

    def _draw_meshy(self, context):
        layout = self.layout
        p = context.scene.meshy

        if not _api_key(context):
            box = layout.box()
            box.label(text="No API key set", icon="ERROR")
            box.operator("preferences.addon_show", text="Open add-on preferences").module = ADDON_ID

        layout.row().prop(p, "mode", expand=True)
        col = layout.column()

        if p.mode == "TEXT":
            col.prop(p, "prompt", text="")
            col.prop(p, "ai_model")
            col.prop(p, "texture")
            if p.texture:
                sub = col.column()
                sub.prop(p, "texture_prompt", text="Texture")
                sub.prop(p, "pbr")
            self._advanced(col, p, negative=True)
            col.operator("meshy.generate", text="Generate from Text", icon="PLAY")

        elif p.mode == "IMAGE":
            col.prop(p, "image_path", text="")
            col.prop(p, "ai_model")
            col.prop(p, "texture")
            if p.texture:
                col.prop(p, "texture_prompt", text="Texture")
                col.prop(p, "pbr")
            self._advanced(col, p)
            col.operator("meshy.generate", text="Generate from Image", icon="PLAY")

        elif p.mode == "MULTI":
            self._views(col, p)
            col.prop(p, "texture")
            if p.texture:
                col.prop(p, "texture_prompt", text="Texture")
                col.prop(p, "pbr")
            self._advanced(col, p)
            col.operator("meshy.generate", text="Generate from Views", icon="PLAY")

        elif p.mode == "TEXTURE":
            self._selection_hint(col, context, p)
            col.prop(p, "texture_prompt", text="Prompt")
            col.prop(p, "style_image", text="Or image")
            col.prop(p, "pbr")
            col.prop(p, "keep_uv")
            col.operator("meshy.generate", text="Texture Selected", icon="BRUSH_DATA")

        elif p.mode == "REMESH":
            self._selection_hint(col, context, p)
            col.prop(p, "topology")
            col.prop(p, "polycount")
            col.operator("meshy.generate", text="Remesh Selected", icon="MOD_REMESH")

        elif p.mode == "RIG":
            self._selection_hint(col, context, p)
            col.prop(p, "rig_type")
            col.prop(p, "height")
            col.prop(p, "with_basic_anims")
            col.operator("meshy.generate", text="Auto-Rig Selected", icon="ARMATURE_DATA")
            col.separator()
            box = col.box()
            box.label(text="Animate rigged character")
            row = box.row(align=True)
            row.prop(p, "action_id")
            row.operator("meshy.open_url", text="", icon="URL").url = \
                "https://www.meshy.ai/animation-library"
            box.operator("meshy.animate", icon="ACTION")

    def _advanced(self, col, p, negative=False):
        box = col.box()
        box.prop(p, "show_advanced", icon="TRIA_DOWN" if p.show_advanced else "TRIA_RIGHT",
                 emboss=False)
        if p.show_advanced:
            if negative:
                box.prop(p, "negative_prompt")
            row = box.row()
            row.enabled = p.ai_model != "meshy-t2.5"
            row.prop(p, "topology")
            box.prop(p, "polycount")
            box.prop(p, "pose_mode")
            if negative:
                box.prop(p, "seed")

    def _selection_hint(self, col, context, p):
        obj = context.active_object
        box = col.box()
        if obj is None:
            box.label(text="Select a model", icon="INFO")
            return
        tid = _find_prop(obj, PROP_TASK_ID)
        box.label(text=obj.name, icon="OBJECT_DATA")
        if tid:
            box.prop(p, "reuse_task")
        else:
            box.label(text="Selection will be uploaded as GLB", icon="EXPORT")

    def _jobs(self, layout):
        if not JOBS:
            return
        box = layout.box()
        row = box.row()
        row.label(text="Jobs")
        if any(j.done for j in JOBS):
            row.operator("meshy.clear_jobs", text="", icon="TRASH")
        for job in JOBS:
            r = box.column(align=True)
            head = r.row()
            label = job.label if len(job.label) < 28 else job.label[:26] + "..."
            if job.error:
                head.label(text=label, icon="ERROR")
                for line in _wrap(job.error, 42)[:4]:
                    r.label(text=line)
            elif job.canceled:
                head.label(text=f"{label} - stopped", icon="CANCEL")
            elif job.done and job.imported:
                head.label(text=f"{label} - done", icon="CHECKMARK")
            else:
                head.label(text=label, icon="TIME")
                head.operator("meshy.cancel", text="", icon="X").job_id = job.id
                status = job.phase
                if job.queue_pos:
                    status += f" (queue {job.queue_pos})"
                if hasattr(r, "progress"):  # Blender 4.0+
                    r.progress(factor=job.progress / 100.0, type="BAR",
                               text=f"{status} {job.progress}%")
                else:
                    r.label(text=f"{status} {job.progress}%")
        box.operator("meshy.reveal", icon="FILE_FOLDER")


def _wrap(text, width):
    words, lines, cur = text.split(), [], ""
    for w in words:
        if len(cur) + len(w) + 1 > width:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    return lines


# ---------------------------------------------------------------------------

CLASSES = (
    MESHY_Preferences,
    MESHY_Props,
    MESHY_OT_generate,
    MESHY_OT_check_server,
    MESHY_OT_animate,
    MESHY_OT_cancel,
    MESHY_OT_clear,
    MESHY_OT_balance,
    MESHY_OT_open_url,
    MESHY_OT_reveal,
    MESHY_OT_retopo_prep,
    MESHY_PT_main,
    MESHY_PT_retopo,
)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.meshy = PointerProperty(type=MESHY_Props)


def unregister():
    if bpy.app.timers.is_registered(_tick):
        bpy.app.timers.unregister(_tick)
    for job in JOBS:
        job.cancel_requested = True
    del bpy.types.Scene.meshy
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
