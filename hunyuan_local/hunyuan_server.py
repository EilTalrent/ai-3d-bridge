"""
Local job server for Hunyuan3D-2GP, made to pair with the "Meshy AI Bridge"
Blender add-on (Local mode).

Place this file in the Hunyuan3D-2GP folder and run it with that folder's
Python environment. It uses mmgp memory offloading so shape + texture
generation fits on ~10 GB GPUs.

Uses Tencent Hunyuan 3D 2.0, licensed under the Tencent Hunyuan 3D 2.0
Community License Agreement, Copyright (c) 2025 Tencent. All Rights Reserved.
The trademark rights of "Tencent Hunyuan" are owned by Tencent or its affiliate.

API (all JSON unless noted):
  GET  /health                -> server info
  POST /jobs                  -> {"id": ...}
       mode: "image" | "text" | "texture" | "multiview"
       image: base64 PNG/JPG (image mode, or texture reference)
       views: {"front"|"back"|"left"|"right": base64} (multiview mode; needs --multiview)
       prompt: str (text mode, or texture description)
       mesh: base64 GLB (texture mode)
       texture: bool, seed: int, steps: int, guidance_scale: float,
       octree_resolution: int, face_count: int
  GET  /jobs/{id}             -> status, phase, progress, error, queue_position
  GET  /jobs/{id}/result      -> model/gltf-binary
  POST /jobs/{id}/cancel
"""

import argparse
import base64
import io
import os
import queue
import threading
import time
import traceback
import uuid

import torch
import trimesh
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from PIL import Image
from pydantic import BaseModel

STATE = {
    "shape_model": None,
    "texture": False,
    "text_to_3d": False,
    "profile": None,
    "multiview": False,
}
JOBS = {}
JOB_QUEUE = queue.Queue()
JOBS_LOCK = threading.Lock()
RESULT_DIR = "server_results"
MAX_KEEP = 40

W = {}  # loaded workers


class Canceled(Exception):
    pass


class JobRequest(BaseModel):
    mode: str = "image"
    image: str | None = None
    prompt: str | None = None
    mesh: str | None = None
    views: dict[str, str] | None = None
    texture: bool = True
    seed: int = 1234
    steps: int | None = None
    guidance_scale: float | None = None
    octree_resolution: int = 256
    face_count: int = 40000


def _decode_image(b64):
    if "," in b64[:100] and b64.startswith("data:"):
        b64 = b64.split(",", 1)[1]
    return Image.open(io.BytesIO(base64.b64decode(b64)))


def _decode_mesh(b64):
    if b64.startswith("data:"):
        b64 = b64.split(",", 1)[1]
    mesh = trimesh.load(io.BytesIO(base64.b64decode(b64)), file_type="glb", force="mesh")
    if not isinstance(mesh, trimesh.Trimesh) or len(mesh.faces) == 0:
        raise ValueError("Uploaded model has no mesh faces.")
    return mesh


def _set(job, **kw):
    with JOBS_LOCK:
        job.update(kw)


def _check_cancel(job):
    if job.get("cancel"):
        raise Canceled()


def _reference_image(job, req):
    """Image for shape/texture conditioning: uploaded, or generated from text."""
    if req.image:
        img = _decode_image(req.image)
    elif req.prompt:
        if not STATE["text_to_3d"]:
            raise ValueError("Text input is disabled. Start the server with --enable_t23d.")
        _set(job, phase="Painting reference image", progress=5)
        img = W["t2i"](req.prompt.strip(), seed=req.seed)
    else:
        raise ValueError("Provide an image or a prompt.")
    _check_cancel(job)
    if img.mode != "RGBA":  # no transparency -> cut out the background
        _set(job, phase="Removing background", progress=max(job["progress"], 12))
        img = W["rmbg"](img.convert("RGB"))
    return img


def _view_images(job, req):
    """Decode multi-view images; cut out backgrounds where there's no transparency."""
    views = {k: v for k, v in (req.views or {}).items() if v and k in ("front", "back", "left", "right")}
    if not views:
        raise ValueError("Add at least one view image.")
    out = {}
    for k, b64 in views.items():
        _check_cancel(job)
        img = _decode_image(b64)
        if img.mode != "RGBA":
            _set(job, phase=f"Removing background ({k})", progress=min(12, job["progress"] + 3))
            img = W["rmbg"](img.convert("RGB"))
        out[k] = img
    return out


def _shape(job, req, image):
    from hy3dgen.shapegen.pipelines import export_to_trimesh

    steps = req.steps or STATE["default_steps"]
    guidance = req.guidance_scale or STATE["default_guidance"]
    p0, p1 = (15, 55) if req.texture else (15, 95)
    _set(job, phase="Generating shape", progress=p0)

    def cb(step, t, outputs):
        _check_cancel(job)
        _set(job, progress=int(p0 + (p1 - p0) * (step + 1) / max(1, steps)))

    if STATE["multiview"] and not isinstance(image, dict):
        image = {"front": image}
    gen = torch.Generator().manual_seed(int(req.seed))
    outputs = W["shape"](
        image=image,
        num_inference_steps=steps,
        guidance_scale=guidance,
        generator=gen,
        octree_resolution=int(req.octree_resolution),
        num_chunks=200000,
        output_type="mesh",
        callback=cb,
        callback_steps=1,
    )
    mesh = export_to_trimesh(outputs)[0]
    _set(job, phase="Cleaning mesh", progress=p1)
    mesh = W["floater"](mesh)
    mesh = W["degenerate"](mesh)
    return mesh


def _texture(job, mesh, image, face_count, pstart):
    if not STATE["texture"]:
        raise ValueError("Texturing is disabled on this server (started with --disable_tex "
                         "or the texture modules failed to load - see the server log).")
    _check_cancel(job)
    _set(job, phase="Reducing faces", progress=pstart)
    mesh = W["reduce"](mesh, max_facenum=int(face_count))
    _check_cancel(job)
    _set(job, phase="Painting texture (slowest step)", progress=pstart + 5)
    return W["tex"](mesh, image)


def run_job(job):
    req = job["req"]
    if req.mode == "texture":
        if not req.mesh:
            raise ValueError("Texture mode needs a mesh.")
        mesh = _decode_mesh(req.mesh)
        image = _reference_image(job, req)
        mesh = _texture(job, mesh, image, req.face_count, 20)
    elif req.mode == "multiview":
        if not STATE["multiview"]:
            raise ValueError("This server runs a single-image model. Restart it with --multiview.")
        views = _view_images(job, req)
        mesh = _shape(job, req, views)
        if req.texture:
            ref = views.get("front") or next(iter(views.values()))
            mesh = _texture(job, mesh, ref, req.face_count, 60)
    elif req.mode in ("image", "text"):
        if req.mode == "text":
            req.image = None
        image = _reference_image(job, req)
        mesh = _shape(job, req, image)
        if req.texture:
            mesh = _texture(job, mesh, image, req.face_count, 60)
    else:
        raise ValueError(f"Unknown mode '{req.mode}'.")

    _set(job, phase="Saving", progress=98)
    path = os.path.join(RESULT_DIR, f"{job['id']}.glb")
    mesh.export(path)
    return path


def worker_loop():
    while True:
        job = JOB_QUEUE.get()
        if job.get("cancel"):
            _set(job, status="canceled", phase="Canceled")
            continue
        _set(job, status="running", started=time.time())
        try:
            path = run_job(job)
            _set(job, status="done", phase="Done", progress=100, result=path)
        except Canceled:
            _set(job, status="canceled", phase="Canceled")
        except torch.cuda.OutOfMemoryError:
            _set(job, status="failed", phase="Failed",
                 error="GPU ran out of memory. Close other GPU apps, lower 'Detail' or "
                       "'Texture faces', or restart the server with a higher --profile.")
        except Exception as e:
            traceback.print_exc()
            _set(job, status="failed", phase="Failed", error=f"{type(e).__name__}: {e}")
        finally:
            job.pop("req", None)  # free uploaded data
            try:
                torch.cuda.empty_cache()
            except Exception:
                pass
            _prune()


def _prune():
    with JOBS_LOCK:
        done = [j for j in JOBS.values() if j["status"] in ("done", "failed", "canceled")]
        done.sort(key=lambda j: j["created"])
        for j in done[:-MAX_KEEP]:  # empty unless more than MAX_KEEP finished
            if j.get("result") and os.path.exists(j["result"]):
                os.remove(j["result"])
            JOBS.pop(j["id"], None)


app = FastAPI(title="Hunyuan3D local server")


@app.get("/health")
def health():
    with JOBS_LOCK:
        busy = any(j["status"] == "running" for j in JOBS.values())
        queued = sum(1 for j in JOBS.values() if j["status"] == "queued")
    return {"ok": True, "shape_model": STATE["shape_model"], "texture": STATE["texture"],
            "text_to_3d": STATE["text_to_3d"], "profile": STATE["profile"],
            "multiview": STATE["multiview"],
            "busy": busy, "queued": queued}


@app.post("/jobs")
def create_job(req: JobRequest):
    if req.mode not in ("image", "text", "texture", "multiview"):
        raise HTTPException(400, f"Unknown mode '{req.mode}'.")
    if req.mode == "multiview" and not STATE["multiview"]:
        raise HTTPException(400, "This server runs a single-image model. "
                                 "Restart it with: start_hunyuan.sh --multiview")
    if req.mode == "text" and not STATE["text_to_3d"]:
        raise HTTPException(400, "Text to 3D is disabled. Start the server with --enable_t23d.")
    if (req.mode == "texture" or req.texture) and not STATE["texture"]:
        raise HTTPException(400, "Texturing is not available on this server.")
    jid = uuid.uuid4().hex[:12]
    job = {"id": jid, "status": "queued", "phase": "Queued", "progress": 0, "error": None,
           "created": time.time(), "req": req, "cancel": False}
    with JOBS_LOCK:
        JOBS[jid] = job
    JOB_QUEUE.put(job)
    return {"id": jid}


@app.get("/jobs/{jid}")
def job_status(jid: str):
    with JOBS_LOCK:
        job = JOBS.get(jid)
        if not job:
            raise HTTPException(404, "No such job.")
        ahead = sum(1 for j in JOBS.values()
                    if j["status"] in ("queued", "running") and j["created"] < job["created"])
        return {k: job.get(k) for k in ("id", "status", "phase", "progress", "error")} | \
            {"queue_position": ahead if job["status"] == "queued" else 0}


@app.get("/jobs/{jid}/result")
def job_result(jid: str):
    job = JOBS.get(jid)
    if not job or job["status"] != "done":
        raise HTTPException(404, "Result not ready.")
    return FileResponse(job["result"], media_type="model/gltf-binary", filename=f"{jid}.glb")


@app.post("/jobs/{jid}/cancel")
def job_cancel(jid: str):
    job = JOBS.get(jid)
    if not job:
        raise HTTPException(404, "No such job.")
    _set(job, cancel=True)
    return JSONResponse({"ok": True})


def _replace_property_getter(instance, name, getter):
    cls = type(instance)
    custom = type(f"Custom{cls.__name__}", (cls,), {})
    setattr(custom, name, property(getter, getattr(cls, name).fset))
    instance.__class__ = custom
    return instance


def load_models(args):
    from mmgp import offload
    from hy3dgen.rembg import BackgroundRemover
    from hy3dgen.shapegen import (DegenerateFaceRemover, FaceReducer, FloaterRemover,
                                  Hunyuan3DDiTFlowMatchingPipeline)

    torch.set_default_device("cpu")
    subfolder = args.subfolder + ("-turbo" if args.turbo else "")
    print(f"Loading shape model {args.model_path}/{subfolder} ...", flush=True)
    W["shape"] = Hunyuan3DDiTFlowMatchingPipeline.from_pretrained(
        args.model_path, subfolder=subfolder, use_safetensors=True, device=args.device)
    if args.turbo or args.enable_flashvdm:
        W["shape"].enable_flashvdm(mc_algo=args.mc_algo)
    STATE["shape_model"] = f"{args.model_path}/{subfolder}"
    STATE["multiview"] = "2mv" in args.model_path.lower()
    STATE["default_steps"] = 5 if args.turbo else 30
    STATE["default_guidance"] = 5.0 if args.turbo else 7.5

    W["rmbg"] = BackgroundRemover()
    W["floater"] = FloaterRemover()
    W["degenerate"] = DegenerateFaceRemover()
    W["reduce"] = FaceReducer()

    if not args.disable_tex:
        try:
            from hy3dgen.texgen import Hunyuan3DPaintPipeline
            print("Loading texture model ...", flush=True)
            W["tex"] = Hunyuan3DPaintPipeline.from_pretrained(args.texgen_model_path)
            STATE["texture"] = True
        except Exception as e:
            traceback.print_exc()
            print(f"!! Texture model failed to load ({e}); continuing without texturing.")

    if args.enable_t23d:
        try:
            from hy3dgen.text2image import HunyuanDiTPipeline
            print("Loading text-to-image model ...", flush=True)
            W["t2i"] = HunyuanDiTPipeline("Tencent-Hunyuan/HunyuanDiT-v1.1-Diffusers-Distilled")
            STATE["text_to_3d"] = True
        except Exception as e:
            traceback.print_exc()
            print(f"!! Text model failed to load ({e}); continuing without text input.")

    profile = int(args.profile)
    STATE["profile"] = profile
    _replace_property_getter(W["shape"], "_execution_device", lambda self: "cuda")
    pipe = offload.extract_models("i23d_worker", W["shape"])
    if STATE["texture"]:
        pipe.update(offload.extract_models("texgen_worker", W["tex"]))
        W["tex"].models["multiview_model"].pipeline.vae.use_slicing = True
    if STATE["text_to_3d"]:
        pipe.update(offload.extract_models("t2i_worker", W["t2i"]))
    kwargs = {}
    if profile < 5:
        kwargs["pinnedMemory"] = "i23d_worker/model"
    if profile not in (1, 3):
        kwargs["budgets"] = {"*": args.budget}
    offload.profile(pipe, profile_no=profile, verboseLevel=int(args.verbose), **kwargs)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--host", default="127.0.0.1",
                    help="127.0.0.1 = this computer only (default); 0.0.0.0 = whole network")
    ap.add_argument("--port", type=int, default=8081)
    ap.add_argument("--model_path", default="tencent/Hunyuan3D-2mini")
    ap.add_argument("--subfolder", default="hunyuan3d-dit-v2-mini")
    ap.add_argument("--texgen_model_path", default="tencent/Hunyuan3D-2")
    ap.add_argument("--h2", action="store_true", help="Use the full Hunyuan3D-2 shape model")
    ap.add_argument("--multiview", "--mv", action="store_true",
                    help="Use Hunyuan3D-2mv: build from front/back/left/right views")
    ap.add_argument("--turbo", action="store_true", help="Faster distilled shape model")
    ap.add_argument("--enable_flashvdm", action="store_true")
    ap.add_argument("--mc_algo", default="dmc")
    ap.add_argument("--enable_t23d", action="store_true", help="Enable text input")
    ap.add_argument("--disable_tex", action="store_true")
    ap.add_argument("--profile", default="4",
                    help="mmgp memory profile 1-5 (4 = low RAM + low VRAM, good for 10 GB cards)")
    ap.add_argument("--budget", type=int, default=2200, help="mmgp VRAM budget per model, MB")
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--verbose", default="1")
    args = ap.parse_args()
    if args.h2:
        args.model_path, args.subfolder = "tencent/Hunyuan3D-2", "hunyuan3d-dit-v2-0"
    if args.multiview:
        args.model_path, args.subfolder = "tencent/Hunyuan3D-2mv", "hunyuan3d-dit-v2-mv"

    os.makedirs(RESULT_DIR, exist_ok=True)
    load_models(args)
    threading.Thread(target=worker_loop, daemon=True).start()
    print(f"\n=== Hunyuan3D server ready on http://{args.host}:{args.port} ===\n", flush=True)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
