"""Resident, offline Docling basics or selected CodeFormulaV2 enrichment.

CANONICAL BASIC PROFILE (2026-10-13): use workers=3, layout/table/OCR
batch=4, accelerator threads=4, safe_parser=True, native parser_threads=1,
and rotate each child after at most 20 PDFs. Keep the whole-card ceiling at
8 GiB (gpu_limit_mib=8192), including desktop/background usage. The basic
route defers ordinary region-image crops and keeps PDF/page/bbox provenance;
selected-paper formula enrichment uses the already cached local CodeFormulaV2
route (single process, normally batch 32). The portable profile and exact
command are documented at
docs/verification/local-docling-batch-20261013/README.md and the template
BASIC_PARALLEL3_CONFIG.template.json. Configurations that omit workers keep
the historical single-worker default; do not silently promote them to three.

One isolated child holds one converter/model for the whole pending batch.
Per-PDF atomic receipts survive a native-process crash; resume skips verified
successes. No model API, server, download or full-page image payload is used.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def sha(data):
    return hashlib.sha256(data).hexdigest()


def atomic(path, value):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp-{os.getpid()}")
    temporary.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def gpu_memory():
    try:
        proc = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        return max(int(float(line.strip())) for line in proc.stdout.splitlines() if line.strip())
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def normalize(manifest):
    payload = read(manifest); rows = []; aliases = []; failures = []; seen = {}
    for i, record in enumerate(payload.get("records") or ()):
        pdf = Path(record.get("local_pdf") or record.get("pdf") or "")
        if not pdf.is_absolute(): pdf = Path(manifest).parent / pdf
        try:
            data = pdf.read_bytes(); digest = sha(data)
            if record.get("pdf_sha256") and record["pdf_sha256"] != digest:
                raise ValueError("source_pdf_hash_mismatch")
            if digest not in seen:
                from pypdfium2 import PdfDocument
                with PdfDocument(str(pdf)) as doc:
                    pages = len(doc)
        except Exception as exc:
            failure = {"source_record_index": i, "canonical_paper_id": record.get("canonical_paper_id", ""),
                "source_pdf": str(pdf), "status": "error", "stage": "input", "error": f"{type(exc).__name__}:{exc}"}
            failures.append(failure); aliases.append(failure); continue
        label = str(record.get("label") or digest[:20])
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", label) or label in {".", ".."}:
            raise ValueError("unsafe_batch_label")
        if digest not in seen:
            if label in {row["label"] for row in rows}:
                raise ValueError("duplicate_batch_label")
            row = {"label": label, "local_pdf": str(pdf.resolve()), "pdf_sha256": digest,
                   "pages": pages, "canonical_paper_id": record.get("canonical_paper_id", "")}
            rows.append(row); seen[digest] = row
        aliases.append({"source_record_index": i, "canonical_paper_id": record.get("canonical_paper_id", ""),
                        "label": seen[digest]["label"], "pdf_sha256": digest})
    if not rows and not failures: raise ValueError("local_pdf_batch_is_empty")
    return rows, aliases, failures


def formula_request(args, row):
    source = Path(args.raw_doc_root) / row["label"] / "document.json"
    selection = read(args.selection).get("records") or {}
    requested = sorted(set((selection.get(row["label"]) or {}).get("region_ids") or ()))
    original = read(source)
    available = {t["self_ref"] for t in original.get("texts") or () if t.get("label") == "formula"}
    if not set(requested).issubset(available): raise ValueError("unknown_selected_formula_ref")
    return source, sha(source.read_bytes()), requested


def fingerprint(args, row, config):
    # Scheduling and native-lifetime knobs do not change the document request.
    # Excluding them keeps existing successful receipts reusable when the
    # verified parallel profile is adopted.
    operational = {"gpu_limit_mib", "timeout_seconds", "memory_fraction", "max_native_restarts",
                   "workers", "max_documents_per_worker", "safe_parser"}
    if args.mode == "basic":
        operational.add("backend_options")
    effective = {key: value for key, value in config.items() if key not in operational}
    key = {"mode": args.mode, "pdf_sha256": row["pdf_sha256"], "config": effective,
           "executor_version": "optomind.resident_docling_executor.v1"}
    if args.mode == "formula":
        _, digest, refs = formula_request(args, row)
        key.update(raw_document_sha256=digest, requested_refs=refs)
    return sha(json.dumps(key, sort_keys=True).encode())


def cached(args, row, config, output_root=None):
    output = Path(output_root if output_root is not None else args.output_root) / row["label"]
    receipt = output / "run.json"; document = output / "document.json"
    if not receipt.is_file(): return None
    value = read(receipt)
    if value.get("status") != "success": return None
    expected = fingerprint(args, row, config)
    override = value.get("effective_config_override")
    if override:
        if override != {"backend_options": {"parser_threads": 1, "release_native_memory_every_n_pages": 0}} or value.get("base_request_sha256") != expected:
            raise ValueError("unrecognized_or_incompatible_batch_fallback_profile")
        expected = fingerprint(args, row, {**config, **override})
    if value.get("request_sha256") != expected:
        raise ValueError(f"successful_batch_cache_incompatible:{output}; choose a new output root")
    if not document.is_file() or sha(document.read_bytes()) != value.get("document_sha256"):
        raise ValueError(f"successful_batch_cache_corrupt:{output}")
    if not value.get("complete_page_coverage") or value.get("source_sha256") != row["pdf_sha256"]:
        raise ValueError("batch_cache_source_or_page_mismatch")
    return {**value, "output": str(output), "document_path": str(document), "run_path": str(receipt), "cached": True}


def formula_elements(document, backend, model, refs):
    from docling_core.types.doc import BoundingBox, DocItem
    from docling.datamodel.base_models import ItemAndImageEnrichmentElement
    elements = []
    for item, _level in document.iterate_items():
        if str(item.self_ref) not in refs or not model.is_processable(doc=document, element=item): continue
        if not isinstance(item, DocItem) or not item.prov: continue
        prov = item.prov[0]; box = prov.bbox
        width, height = box.r - box.l, box.t - box.b
        expanded = BoundingBox(l=box.l - width * model.expansion_factor, t=box.t + height * model.expansion_factor,
            r=box.r + width * model.expansion_factor, b=box.b - height * model.expansion_factor, coord_origin=box.coord_origin)
        page = backend.load_page(page_no=prov.page_no - 1)
        elements.append(ItemAndImageEnrichmentElement(item=item, image=page.get_page_image(scale=model.images_scale, cropbox=expanded)))
    return elements


def worker(args, rows, config):
    base_config = config
    if args.safe_parser:
        config = {**config, "backend_options": {"parser_threads": 1, "release_native_memory_every_n_pages": 0}}
    for name in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_DATASETS_OFFLINE"): os.environ[name] = "1"
    os.environ["PYTORCH_CUDA_ALLOC_CONF"] = "expandable_segments:True"
    packages = {}
    for name in set(config.get("packages") or ()) | {"docling", "docling-core", "docling-ibm-models", "docling-parse", "torch", "pypdfium2"}:
        try: packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError: packages[name] = None
    if config.get("packages") and any(packages.get(name) != version for name, version in config["packages"].items()):
        raise ValueError("installed_docling_packages_differ_from_profile")
    import torch
    if torch.cuda.is_available(): torch.cuda.set_per_process_memory_fraction(float(args.memory_fraction or config.get("memory_fraction", .60)))
    from docling.datamodel.base_models import InputFormat
    if args.mode == "basic":
        from scripts.upgrade3.parse_docling_pdf import native_pipeline_options
        from docling.document_converter import DocumentConverter, PdfFormatOption
        options = config["pipeline_options"]
        for name in ("do_formula_enrichment", "do_code_enrichment", "do_picture_description", "enable_remote_services", "allow_external_plugins"):
            if options.get(name): raise ValueError(f"basic_profile_cannot_enable:{name}")
        if not Path(options["artifacts_path"]).is_dir(): raise ValueError("local_docling_model_cache_missing")
        pipeline, _ = native_pipeline_options(options)
        effective_options = pipeline.model_dump(mode="json", serialize_as_any=True)
        format_options = {"pipeline_options": pipeline}
        if config.get("backend_options"):
            from docling.datamodel.backend_options import ThreadedDoclingParseBackendOptions
            format_options["backend_options"] = ThreadedDoclingParseBackendOptions(**config["backend_options"])
        converter = DocumentConverter(allowed_formats=[InputFormat.PDF], format_options={InputFormat.PDF: PdfFormatOption(**format_options)})
    else:
        from docling.backend.pypdfium2_backend import PyPdfiumDocumentBackend
        from docling.datamodel.accelerator_options import AcceleratorOptions
        from docling.datamodel.document import InputDocument
        from docling.datamodel.pipeline_options import CodeFormulaVlmOptions
        from docling_core.types.doc import DoclingDocument
        from docling.models.stages.code_formula.code_formula_vlm_model import CodeFormulaVlmModel
        model_options = CodeFormulaVlmOptions.from_preset("codeformulav2")
        model = CodeFormulaVlmModel(enabled=True, enable_remote_services=False,
            artifacts_path=Path(config["model_cache"]), options=model_options,
            accelerator_options=AcceleratorOptions(device=config.get("device", "cuda")))
        effective_options = {"name": "CodeFormulaV2", "options": model_options.model_dump(mode="json", serialize_as_any=True),
                             "batch_size": int(config.get("batch_size", 32)), "packages": packages,
                             "model_manifest": config.get("model_manifest"), "model_cache": config["model_cache"]}
    for row in rows:
        if args.resume and cached(args, row, base_config): continue
        started = time.monotonic(); output = Path(args.output_root) / row["label"]
        output.mkdir(parents=True, exist_ok=True)
        receipt = {"mode": args.mode, "status": "started", "label": row["label"],
            "source_sha256": row["pdf_sha256"], "source_pdf": row["local_pdf"], "source_page_count": row["pages"],
            "page_range": [1, row["pages"]], "packages": packages, "request_sha256": fingerprint(args, row, config),
            "execution_id": args.execution_id, "runner_sha256": sha(Path(__file__).read_bytes())}
        if args.safe_parser:
            receipt.update(base_request_sha256=fingerprint(args, row, base_config),
                effective_config_override={"backend_options": config["backend_options"]})
        atomic(output / "run.json", receipt)
        try:
            if args.mode == "basic":
                converted = converter.convert(Path(row["local_pdf"]), raises_on_error=True)
                payload = converted.document.export_to_dict()
                status = getattr(converted.status, "value", str(converted.status))
                if status != "success": raise ValueError(f"docling_partial_conversion:{status}")
                receipt["pipeline_options"] = effective_options
                receipt["backend"] = "ThreadedDoclingParseDocumentBackend"
                receipt["backend_options"] = config.get("backend_options") or "installed_default"
            else:
                source, raw_hash, requested = formula_request(args, row)
                document = DoclingDocument.load_from_json(source)
                in_doc = InputDocument(Path(row["local_pdf"]), format=InputFormat.PDF, backend=PyPdfiumDocumentBackend, filename=Path(row["local_pdf"]).name)
                elements = formula_elements(document, in_doc._backend, model, set(requested))
                processed = []
                batch_size = int(config.get("batch_size", 32))
                if not 1 <= batch_size <= 64: raise ValueError("local_formula_batch_size_invalid")
                try:
                    for offset in range(0, len(elements), batch_size):
                        batch = elements[offset:offset + batch_size]
                        for _ in model(doc=document, element_batch=batch): pass
                        processed.extend(str(e.item.self_ref) for e in batch)
                finally:
                    in_doc._backend.unload()
                payload = document.export_to_dict()
                empty = [item["self_ref"] for item in payload.get("texts") or () if item.get("self_ref") in processed and not str(item.get("text") or "").strip()]
                receipt.update(raw_document_sha256=raw_hash, requested_refs=requested, processed_refs=processed,
                    unprocessed_refs=sorted(set(requested) - set(processed)), empty_result_refs=empty, model=effective_options)
            pages = sorted(int(p) for p in payload.get("pages") or {})
            if pages != list(range(1, row["pages"] + 1)): raise ValueError("native_document_page_coverage_incomplete")
            atomic(output / "document.json", payload)
            document_sha = sha((output / "document.json").read_bytes())
            complete = not receipt.get("unprocessed_refs") and not receipt.get("empty_result_refs")
            receipt.update(status="success" if complete else "partial", complete_page_coverage=True, document_sha256=document_sha,
                enhanced_document_sha256=document_sha if args.mode == "formula" else None,
                elapsed_seconds=round(time.monotonic() - started, 3))
        except Exception as exc:
            receipt.update(status="error", error=f"{type(exc).__name__}:{exc}", elapsed_seconds=round(time.monotonic() - started, 3))
        atomic(output / "run.json", receipt)
        # A failed native conversion may leave pipeline queues/producers alive.
        # Publish its terminal receipt, then let the parent continue siblings in
        # a fresh child. Successful conversions still share one resident model.
        if receipt["status"] != "success": return 0
    return 0


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--mode", choices=("basic", "formula"), required=True)
    p.add_argument("--manifest", type=Path, required=True); p.add_argument("--config", type=Path, required=True)
    p.add_argument("--output-root", type=Path, required=True); p.add_argument("--resume", action="store_true")
    p.add_argument("--workers", type=int, help="basic worker processes; defaults to the config value or 1")
    p.add_argument("--raw-doc-root", type=Path); p.add_argument("--selection", type=Path)
    p.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--memory-fraction", type=float, help=argparse.SUPPRESS)
    p.add_argument("--execution-id", default="", help=argparse.SUPPRESS)
    p.add_argument("--worker-labels", type=Path, help=argparse.SUPPRESS)
    p.add_argument("--safe-parser", action="store_true", help=argparse.SUPPRESS)
    return p


def basic_safe_parser(config, args):
    """Use the bounded native parser profile for every basic child.

    The profile is the stable route for concurrent resident converters.  The
    explicit flag remains accepted for recovery/history compatibility; the
    production basic route now opts in automatically without changing formula
    enrichment behavior.
    """
    return args.mode == "basic" and (getattr(args, "safe_parser", False) or config.get("safe_parser", True))


def worker_batch_limit(config, args):
    """Bound basic child lifetime while retaining per-PDF cache reuse."""
    if args.mode != "basic":
        return None
    value = config.get("max_documents_per_worker", 20)
    try:
        value = int(value)
    except (TypeError, ValueError):
        raise ValueError("max_documents_per_worker_invalid")
    if value < 1:
        raise ValueError("max_documents_per_worker_must_be_positive")
    return min(value, 1000)


def batch_worker_count(config, args):
    """Return the parent basic fan-out; formula enrichment stays serial."""
    if args.mode != "basic":
        return 1
    value = getattr(args, "workers", None)
    if value is None:
        # Historical configs omit this operational knob. Keep their old
        # single-resident-process behavior; the verified three-process route
        # opts in through an explicit config or --workers 3.
        value = config.get("workers", 1)
    try:
        value = int(value)
    except (TypeError, ValueError):
        raise ValueError("workers_invalid")
    if value < 1 or value > 8:
        raise ValueError("workers_must_be_between_1_and_8")
    return value


def process_alive(pid):
    if pid <= 0: return False
    if os.name == "nt":
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not handle: return ctypes.windll.kernel32.GetLastError() == 5
        try:
            code = ctypes.c_ulong()
            return bool(ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try: os.kill(pid, 0); return True
    except ProcessLookupError: return False
    except PermissionError: return True


def acquire_lock(path):
    path = Path(path)
    for _ in range(3):
        try:
            with path.open("x", encoding="utf-8") as lock:
                lock.write(json.dumps({"pid": os.getpid(), "started_at": time.time()}))
            return
        except FileExistsError:
            old = path.read_bytes(); value = json.loads(old)
            pid = int(value.get("pid", 0) if isinstance(value, dict) else value)
            if process_alive(pid): raise ValueError(f"batch_output_is_locked_by_active_pid:{pid}")
            if path.read_bytes() == old:
                os.replace(path, path.with_name(path.name + ".stale-" + uuid.uuid4().hex))
    raise ValueError("batch_lock_changed_concurrently")


def allocator_fraction(total, background, ceiling=8192, requested=.60):
    available = int(ceiling) - int(background) - 512
    if available <= 0 or total <= 0: raise ValueError("insufficient_gpu_headroom")
    return min(float(requested), available / int(total))


def gpu_allocator(config):
    try:
        proc = subprocess.run(["nvidia-smi", "--query-gpu=memory.total,memory.used", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        total, used = [int(float(x.strip())) for x in proc.stdout.splitlines()[0].split(",")]
        return allocator_fraction(total, used, config.get("gpu_limit_mib", 8192), config.get("memory_fraction", .60))
    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
        raise ValueError("gpu_memory_inventory_unavailable_or_no_headroom")


def native_crash(code):
    return code in {-6, -7, -11, 0xC0000005, 0xC0000374, 0xC0000409, 0xC000001D, 0xC00000FD}


def stop_owned_worker(proc):
    """Windows venv launchers may own a second, real Python/GPU process."""
    if proc.poll() is not None: return
    if os.name == "nt":
        result = subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
            capture_output=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode and proc.poll() is None:
            raise RuntimeError(f"owned_worker_tree_cleanup_failed:{proc.pid}")
    else:
        proc.kill()
    proc.wait(timeout=15)


def run_isolated_worker(command, output_root, config, started, samples, history_entry):
    env = {**os.environ, "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "HF_DATASETS_OFFLINE": "1"}
    aborted = None
    with (output_root / "stdout.log").open("ab") as stdout, (output_root / "stderr.log").open("ab") as stderr:
        proc = subprocess.Popen(command, stdout=stdout, stderr=stderr, env=env,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        history_entry["pid"] = proc.pid
        atomic(output_root / "CURRENT_WORKER.json", history_entry)
        try:
            while proc.poll() is None:
                used = gpu_memory(); samples.append(used)
                if used is not None and used > int(config.get("gpu_limit_mib", 8192)):
                    aborted = "whole_gpu_memory_ceiling_exceeded"; stop_owned_worker(proc); break
                if time.monotonic() - started > float(config.get("timeout_seconds", 1800)):
                    aborted = "batch_deadline_exceeded"; stop_owned_worker(proc); break
                time.sleep(.5)
            code = proc.wait(timeout=15)
        finally:
            if proc.poll() is None: stop_owned_worker(proc)
    history_entry.update(status="finished", returncode=code, abort_reason=aborted, finished_at=time.time())
    atomic(output_root / "CURRENT_WORKER.json", history_entry)
    return code, aborted


def recover_workers(args, rows, config, started, samples, original_argv):
    """Retry native process faults only, without overwriting a successful PDF.

    A repeated culprit is isolated from the resident queue, then gets one basic
    attempt with one native parser thread. Timeout, ceiling and operator stops
    never enter recovery. All original receipts are retained in worker history.
    """
    history_path = args.output_root / "WORKER_HISTORY.json"
    history = read(history_path) if history_path.is_file() else []
    current_history = []; quarantined = set(); safe_queue = []; failures = {}
    max_restarts = max(0, min(10, int(config.get("max_native_restarts", 3))))
    # Keep terminal quarantines across unattended resumes. Retrying one needs a
    # deliberate new output root; successful sibling PDFs remain reusable.
    for row in rows:
        path = args.output_root / row["label"] / "run.json"
        if path.is_file() and read(path).get("status") == "quarantined": quarantined.add(row["label"])
    bounded_profile = basic_safe_parser(config, args)
    batch_limit = worker_batch_limit(config, args)
    code = 0; aborted = None; restarts = 0; consecutive_no_progress = 0; execution_ids = set()
    while True:
        pending = [row for row in rows if row["label"] not in quarantined and not cached(args, row, config)]
        # Python-level per-document failures are terminal for this invocation.
        pending = [row for row in pending if not any(row["label"] in h.get("terminal_labels", []) for h in current_history)]
        safe = False
        if not pending:
            if not safe_queue: break
            pending = [safe_queue.pop(0)]; safe = True
        elif batch_limit is not None:
            pending = pending[:batch_limit]
        if time.monotonic() - started > float(config.get("timeout_seconds", 1800)):
            aborted = "batch_deadline_exceeded"; break
        args.execution_id = uuid.uuid4().hex; execution_ids.add(args.execution_id)
        labels_path = args.output_root / ".workers" / f"{args.execution_id}.json"
        atomic(labels_path, [row["label"] for row in pending])
        entry = {"execution_id": args.execution_id, "status": "starting", "started_at": time.time(),
            "labels": [row["label"] for row in pending], "safe_parser": bool(bounded_profile or safe),
            "parser_profile": "single_thread" if (bounded_profile or safe) else "installed_default",
            "max_documents_per_worker": batch_limit, "restart_number": restarts}
        history.append(entry); current_history.append(entry); atomic(history_path, history)
        command = [sys.executable, "-B", "-X", "utf8", str(Path(__file__).resolve()), *original_argv,
            "--worker", "--resume", "--worker-labels", str(labels_path), "--memory-fraction", str(gpu_allocator(config)),
            "--execution-id", args.execution_id]
        if bounded_profile or safe: command.append("--safe-parser")
        code, aborted = run_isolated_worker(command, args.output_root, config, started, samples, entry)
        receipts = []
        for row in pending:
            path = args.output_root / row["label"] / "run.json"
            if path.is_file():
                receipt = read(path)
                if receipt.get("execution_id") == args.execution_id: receipts.append(receipt)
        entry["receipts"] = receipts
        entry["terminal_labels"] = [r["label"] for r in receipts if r.get("status") in {"error", "partial"}]
        culprits = [r for r in receipts if r.get("status") == "started"]
        entry["started_culprits"] = [r["label"] for r in culprits]
        if code == 0 and aborted is None:
            missing = [row["label"] for row in pending if row["label"] not in entry["terminal_labels"] and not cached(args, row, config)]
            if missing:
                entry["no_progress_labels"] = missing
                aborted = "worker_no_progress"
        if aborted or not native_crash(code):
            atomic(history_path, history)
            if aborted or code != 0: break
            continue
        isolated = False
        for receipt in culprits:
            label = receipt["label"]; failures[label] = failures.get(label, 0) + 1
            if safe or failures[label] >= 2:
                isolated = True
                quarantined.add(label)
                atomic(args.output_root / label / "run.json", {**receipt, "status": "quarantined",
                    "error": "repeated_native_worker_crash" if not safe else "native_worker_crash_in_safe_parser",
                    "native_returncode": code, "native_failures_this_run": failures[label]})
                if args.mode == "basic" and not safe:
                    safe_queue.extend(row for row in pending if row["label"] == label)
        atomic(history_path, history)
        if safe:
            continue
        progress = isolated or any(r.get("status") == "success" for r in receipts)
        consecutive_no_progress = 0 if progress else consecutive_no_progress + 1
        entry["consecutive_no_progress_crashes"] = consecutive_no_progress
        atomic(history_path, history)
        if max_restarts == 0 or consecutive_no_progress > max_restarts:
            aborted = "native_restart_limit_exhausted"; break
        restarts += 1
    return code, aborted, current_history, execution_ids


def promote_staged_outputs(args, rows, config):
    """Promote only cache-compatible completed shard outputs."""
    staging = args.output_root / ".workers"
    rows_by_label = {row["label"]: row for row in rows}
    if not staging.is_dir():
        return []
    promoted = []
    for session in sorted(staging.iterdir(), key=lambda path: path.name):
        if not session.is_dir():
            continue
        for worker_root in sorted(session.iterdir(), key=lambda path: path.name):
            if not worker_root.is_dir():
                continue
            for label_root in sorted(worker_root.iterdir(), key=lambda path: path.name):
                row = rows_by_label.get(label_root.name)
                if row is None or not label_root.is_dir():
                    continue
                if not (label_root / "run.json").is_file():
                    continue
                target = args.output_root / label_root.name
                try:
                    staged_valid = cached(args, row, config, worker_root)
                except ValueError:
                    staged_valid = None
                if staged_valid is None:
                    continue
                try:
                    target_valid = cached(args, row, config)
                except ValueError:
                    target_valid = None
                if target_valid is not None:
                    continue
                if target.exists():
                    stale = target.with_name(target.name + ".stale-" + uuid.uuid4().hex)
                    shutil.move(str(target), str(stale))
                shutil.move(str(label_root), str(target))
                promoted.append(label_root.name)
    return promoted


def split_rows(rows, worker_count):
    count = min(max(1, int(worker_count)), len(rows))
    groups = [[] for _ in range(count)]
    offset = 0
    for index in range(count):
        remaining = len(rows) - offset
        slots = count - index
        size = (remaining + slots - 1) // slots
        groups[index].extend(rows[offset:offset + size])
        offset += size
    return [group for group in groups if group]


def run_parallel_workers(args, rows, config, started, samples, original_argv, worker_count):
    """Run disjoint basic shards concurrently and flatten compatible receipts."""
    session = args.output_root / ".workers" / f"parallel-{uuid.uuid4().hex}"
    session.mkdir(parents=True, exist_ok=True)
    config_path = session / "config.json"
    atomic(config_path, config)
    groups = split_rows(rows, worker_count)
    children = []
    completed = False
    cleanup_errors = []

    def cleanup_children():
        for child in children:
            proc = child.get("proc")
            if proc is not None and proc.poll() is None:
                try:
                    stop_owned_worker(proc)
                except Exception as exc:
                    cleanup_errors.append(f"{type(exc).__name__}:{exc}")
            for stream_name in ("stdout", "stderr"):
                stream = child.get(stream_name)
                if stream is not None:
                    try:
                        stream.close()
                    except Exception:
                        pass

    try:
        for index, group in enumerate(groups, start=1):
            worker_root = session / f"worker_{index}"
            worker_root.mkdir(parents=True, exist_ok=True)
            shard_manifest = worker_root / "manifest.json"
            atomic(
                shard_manifest,
                {"records": [
                    {"label": row["label"], "local_pdf": row["local_pdf"],
                     "pdf_sha256": row["pdf_sha256"], "canonical_paper_id": row.get("canonical_paper_id", "")}
                    for row in group
                ]},
            )
            stdout = stderr = None
            try:
                stdout = (worker_root / "stdout.log").open("ab")
                stderr = (worker_root / "stderr.log").open("ab")
                command = [
                    sys.executable, "-B", "-X", "utf8", str(Path(__file__).resolve()),
                    *original_argv,
                    "--manifest", str(shard_manifest), "--config", str(config_path),
                    "--output-root", str(worker_root), "--resume", "--workers", "1",
                ]
                proc = subprocess.Popen(
                    command, stdout=stdout, stderr=stderr,
                    env={**os.environ, "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "HF_DATASETS_OFFLINE": "1"},
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
            except BaseException:
                for stream in (stdout, stderr):
                    if stream is not None:
                        stream.close()
                raise
            children.append({
                "index": index, "group": group, "worker_root": worker_root,
                "proc": proc, "stdout": stdout, "stderr": stderr,
                "command": command, "started": time.monotonic(),
                "returncode": None, "abort_reason": None, "result": None,
            })

        aborted = None
        peak = None
        timeout = float(config.get("timeout_seconds", 1800) or 1800)
        while any(child["proc"].poll() is None for child in children):
            used = gpu_memory()
            samples.append(used)
            if isinstance(used, int):
                peak = max(peak or used, used)
            limit = int(config.get("gpu_limit_mib", 8192) or 8192)
            if isinstance(used, int) and used > limit:
                aborted = f"whole_gpu_memory_ceiling_exceeded:{used}>{limit}MiB"
            elif time.monotonic() - started > timeout:
                aborted = f"batch_deadline_exceeded:{timeout}s"
            if aborted:
                for child in children:
                    if child["proc"].poll() is None:
                        child["abort_reason"] = aborted
                        stop_owned_worker(child["proc"])
                break
            time.sleep(.5)

        aggregate_history = []
        execution_ids = set()
        records_by_label = {}
        child_summaries = []
        child_codes = []
        for child in children:
            proc = child["proc"]
            try:
                code = proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                stop_owned_worker(proc)
                code = proc.wait(timeout=15)
            child["returncode"] = code
            child_codes.append(code)
            result_path = child["worker_root"] / "BATCH_RESULT.json"
            result = read(result_path) if result_path.is_file() else None
            child["result"] = result
            if result:
                child_peak = result.get("peak_gpu_memory_mib")
                if isinstance(child_peak, int):
                    peak = max(peak or child_peak, child_peak)
                for record in result.get("records") or []:
                    label = record.get("label")
                    if label:
                        records_by_label[label] = dict(record)
            history_path = child["worker_root"] / "WORKER_HISTORY.json"
            history = read(history_path) if history_path.is_file() else []
            for entry in history:
                entry = dict(entry)
                entry["parallel_worker"] = child["index"]
                aggregate_history.append(entry)
                if entry.get("execution_id"):
                    execution_ids.add(entry["execution_id"])
            for row in child["group"]:
                label = row["label"]
                staged = child["worker_root"] / label
                target = args.output_root / label
                staged_valid = None
                if staged.is_dir() and (staged / "run.json").is_file():
                    try:
                        staged_valid = cached(args, row, config, child["worker_root"])
                    except ValueError:
                        staged_valid = None
                try:
                    target_valid = cached(args, row, config)
                except ValueError:
                    target_valid = None
                if staged_valid is not None and target_valid is None:
                    if target.exists():
                        stale = target.with_name(target.name + ".stale-" + uuid.uuid4().hex)
                        shutil.move(str(target), str(stale))
                    shutil.move(str(staged), str(target))
                    target_valid = cached(args, row, config)
                record = target_valid or records_by_label.get(label)
                if record is not None:
                    record["output"] = str(target)
                    record["output_root"] = str(target)
                    record["document_path"] = str(target / "document.json")
                    record["run_path"] = str(target / "run.json")
                    records_by_label[label] = record
            reason = child["abort_reason"] or (result or {}).get("abort_reason")
            child_summaries.append({
                "index": child["index"], "labels": [row["label"] for row in child["group"]],
                "returncode": code, "abort_reason": reason,
                "wall_seconds": round(time.monotonic() - child["started"], 3),
                "status": (result or {}).get("status", "missing"),
                "worker_root": str(child["worker_root"]),
                "worker_execution_count": (result or {}).get("worker_execution_count", len(history)),
                "native_crash_count": (result or {}).get("native_crash_count", 0),
                "peak_gpu_memory_mib": (result or {}).get("peak_gpu_memory_mib"),
            })
        if aborted is None:
            failed = [summary for summary in child_summaries if summary["returncode"] != 0 or summary["status"] != "success"]
            if failed:
                aborted = next((summary["abort_reason"] for summary in failed if summary["abort_reason"]), "parallel_child_failed")
        code = 0 if aborted is None else next((value for value in child_codes if value != 0), 1)
        completed = True
        return {
            "code": code, "aborted": aborted, "history": aggregate_history,
            "execution_ids": execution_ids, "records": records_by_label,
            "children": child_summaries, "peak_gpu_memory_mib": peak,
            "worker_count": len(groups),
        }
    finally:
        cleanup_children()
        if completed and cleanup_errors:
            raise RuntimeError("parallel_owned_worker_cleanup_failed:" + ";".join(cleanup_errors))


def main(argv=None):
    args = parser().parse_args(argv)
    args.output_root = args.output_root.resolve()
    args.manifest = args.manifest.resolve(); args.config = args.config.resolve()
    if args.raw_doc_root: args.raw_doc_root = args.raw_doc_root.resolve()
    if args.selection: args.selection = args.selection.resolve()
    if args.mode == "formula" and not (args.raw_doc_root and args.selection): raise ValueError("formula_requires_raw_document_and_selection")
    config = read(args.config)
    for container, key in ((config.get("pipeline_options") or {}, "artifacts_path"), (config, "model_cache")):
        if container.get(key):
            path = Path(container[key]); container[key] = str(path.resolve() if path.is_absolute() else (args.config.parent / path).resolve())
    config["gpu_limit_mib"] = min(8192, int(config.get("gpu_limit_mib", 8192)))
    rows, aliases, input_failures = normalize(args.manifest)
    worker_count = batch_worker_count(config, args)
    if args.worker:
        if args.worker_labels:
            selected = set(read(args.worker_labels)); rows = [row for row in rows if row["label"] in selected]
        return worker(args, rows, config)
    args.output_root.mkdir(parents=True, exist_ok=True)
    lock_path = args.output_root / ".batch.lock"
    acquire_lock(lock_path)
    started = time.monotonic(); samples = []; code = 0; aborted = None; worker_history = []; execution_ids = set()
    parallel_children = []
    parallel_peak = None
    try:
        # A previous parent may have finished a shard before it was able to
        # flatten the stable labels. Reuse those receipts before scheduling.
        promote_staged_outputs(args, rows, config)
        done = [cached(args, row, config) if args.resume else None for row in rows]
        if not args.resume and any((args.output_root / row["label"] / "run.json").exists() for row in rows):
            raise ValueError("existing_batch_results_require_resume")
        if not all(done):
            original_argv = list(argv if argv is not None else sys.argv[1:])
            if args.mode == "basic" and worker_count > 1:
                parallel = run_parallel_workers(args, rows, config, started, samples, original_argv, worker_count)
                code, aborted = parallel["code"], parallel["aborted"]
                worker_history, execution_ids = parallel["history"], parallel["execution_ids"]
                parallel_children, parallel_peak = parallel["children"], parallel["peak_gpu_memory_mib"]
                atomic(args.output_root / "WORKER_HISTORY.json", worker_history)
            else:
                code, aborted, worker_history, execution_ids = recover_workers(args, rows, config, started, samples,
                    original_argv)
        results = []
        for row_index, row in enumerate(rows):
            value = cached(args, row, config)
            if value is None:
                run = args.output_root / row["label"] / "run.json"
                value = read(run) if run.exists() else {"label": row["label"], "source_sha256": row["pdf_sha256"], "status": "not_completed"}
            value["cached"] = bool(done[row_index])
            results.append(value)
        success = not input_failures and all(r.get("status") == "success" for r in results)
        report = {"schema_version": "optomind.docling_batch.v1", "mode": args.mode,
            "status": "success" if success and code == 0 and aborted is None else "partial", "records": results + input_failures,
            "source_records": aliases, "unique_pdf_count": len(rows), "source_record_count": len(aliases),
            "cached_count": sum(bool(r) for r in done), "planned_count": sum(r is None for r in done),
            "attempted_count": sum(r.get("execution_id") in execution_ids for r in results),
            "reconverted_count": sum(r.get("status") == "success" and not previous for r, previous in zip(results, done)),
            "elapsed_seconds": round(time.monotonic() - started, 3), "worker_returncode": code, "abort_reason": aborted,
            "native_crash_count": sum(native_crash(h["returncode"]) for h in worker_history),
            "worker_execution_count": len(worker_history), "worker_history_path": str(args.output_root / "WORKER_HISTORY.json"),
            "workers": worker_count, "parallel_children": parallel_children,
            "peak_gpu_memory_mib": max([x for x in samples if x is not None] + ([parallel_peak] if parallel_peak is not None else []), default=None)}
        if worker_history:
            atomic(args.output_root / "WORKER_HISTORY.json", worker_history)
        atomic(args.output_root / "BATCH_RESULT.json", report)
        print(json.dumps({key: report[key] for key in ("status", "unique_pdf_count", "cached_count", "reconverted_count", "elapsed_seconds", "worker_returncode")}, ensure_ascii=True))
        return 0 if report["status"] == "success" else 2
    finally:
        lock_path.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
