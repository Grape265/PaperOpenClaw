"""Command-line entry point: ``python -m paperopenclaw <command>``.

Commands:

    serve        start the API + frontend server (default)
    ingest       ingest PDFs or directories from the command line
    doctor       print environment diagnostics
    okf-verify   check an OKF bundle for conformance
    info         print resolved configuration and data directory

The CLI exists because the pipeline and the server share one composition root;
anything the UI can do to the corpus can therefore also be scripted.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import __version__


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="paperopenclaw",
        description="PaperOpenClaw：论文知识库（OKF + 金字塔 RAG + 鱼骨知识图谱）",
    )
    parser.add_argument("--data-dir", default=None,
                        help="数据目录（默认取 PAPEROPENCLAW_DATA_DIR 或 ./data）")
    parser.add_argument("--version", action="version", version=f"paperopenclaw {__version__}")

    # `--data-dir` is accepted on both sides of the subcommand. Users naturally
    # write `paperopenclaw serve --data-dir X`, which the root parser alone would
    # reject; `SUPPRESS` as the default stops the subparser from clobbering a
    # value the root parser already captured.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--data-dir", default=argparse.SUPPRESS,
                        help="数据目录（可写在子命令之前或之后）")

    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", parents=[common], help="启动服务")
    serve.add_argument("--host", default=None)
    serve.add_argument("--port", type=int, default=None)
    serve.add_argument("--reload", action="store_true", help="开发模式自动重载")
    serve.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")

    ingest = sub.add_parser("ingest", parents=[common], help="从命令行导入 PDF")
    ingest.add_argument("paths", nargs="+", help="PDF 文件或目录")
    ingest.add_argument("--recursive", action="store_true", default=True)
    ingest.add_argument("--no-recursive", dest="recursive", action="store_false")
    ingest.add_argument("--force", action="store_true", help="即使已存在也重新解析")
    ingest.add_argument("--json", action="store_true", help="以 JSON 输出结果")

    sub.add_parser("doctor", parents=[common], help="环境诊断")

    verify = sub.add_parser("okf-verify", parents=[common], help="校验 OKF Bundle 一致性")
    verify.add_argument("paths", nargs="+", help="OKF Bundle 目录")

    sub.add_parser("info", parents=[common], help="打印配置与统计")

    rebuild = sub.add_parser("rebuild-kg", parents=[common], help="从 RAG 库 B 重建知识图谱")
    rebuild.add_argument("--hashes", default="", help="逗号分隔的论文哈希；不填则重建全部")

    return parser


def _cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .config import bootstrap_settings
    from .gateway.app import create_app

    paths, store = bootstrap_settings(args.data_dir)
    settings = store.current

    host = args.host or settings.server.host
    port = int(args.port or settings.server.port)
    url = f"http://{host}:{port}"

    print(f"PaperOpenClaw {__version__}")
    print(f"  数据目录：{paths.root}")
    print(f"  访问地址：{url}")
    print(f"  接口文档：{url}/docs")
    print("  按 Ctrl+C 停止")

    if settings.server.open_browser and not args.no_browser:
        _open_browser_later(url)

    app = create_app(str(paths.root))
    uvicorn.run(app, host=host, port=port, log_level="info", access_log=False)
    return 0


def _open_browser_later(url: str, delay: float = 1.5) -> None:
    import threading
    import webbrowser

    def open_it() -> None:
        import time

        time.sleep(delay)
        try:
            webbrowser.open(url)
        except Exception:  # pragma: no cover - headless environment
            pass

    threading.Thread(target=open_it, daemon=True).start()


def _cmd_ingest(args: argparse.Namespace) -> int:
    from .services import create_services

    services = create_services(args.data_dir)
    candidates: List[Path] = []
    for raw in args.paths:
        path = Path(raw).expanduser()
        if path.is_dir():
            pattern = "**/*.pdf" if args.recursive else "*.pdf"
            candidates.extend(sorted(path.glob(pattern)))
        elif path.is_file():
            candidates.append(path)
        else:
            print(f"跳过不存在的路径：{raw}", file=sys.stderr)

    if not candidates:
        print("没有找到任何 PDF 文件", file=sys.stderr)
        return 1

    results: List[Dict[str, Any]] = []
    failures = 0
    for path in candidates:
        try:
            outcome = services.ingest.ingest_path(path, force=args.force)
            results.append(outcome)
            if not args.json:
                print(f"[完成] {path.name} -> {outcome['hash_short']} {outcome.get('title', '')}")
        except Exception as exc:  # noqa: BLE001 - report and continue
            failures += 1
            results.append({"file": str(path), "error": str(exc)})
            print(f"[失败] {path.name}: {exc}", file=sys.stderr)

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        print(f"\n共 {len(candidates)} 个文件，成功 {len(candidates) - failures}，失败 {failures}")
    services.close()
    return 1 if failures else 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    from .services import create_services

    services = create_services(args.data_dir)
    report = services.doctor()
    print(f"PaperOpenClaw {__version__} 诊断")
    print(f"数据目录：{services.paths.root}\n")
    for check in report["checks"]:
        mark = "OK  " if check["ok"] else "FAIL"
        detail = f"  {check['detail']}" if check["detail"] else ""
        print(f"  [{mark}] {check['name']}{detail}")
    print("\n数据统计：")
    for key, value in sorted(report["counts"].items()):
        print(f"  {key}: {value}")
    services.close()
    return 0 if report["ok"] else 1


def _cmd_okf_verify(args: argparse.Namespace) -> int:
    from .memory.okf_fs import validate_bundle

    exit_code = 0
    for raw in args.paths:
        bundle = Path(raw).expanduser()
        report = validate_bundle(bundle)
        status = "符合规范" if report["conformant"] else "不符合规范"
        print(f"{bundle}: {status}（概念文档 {report['files']} 个）")
        for item in report["errors"]:
            print(f"  错误 {item['file']}: {item['message']}")
        for item in report["warnings"][:20]:
            print(f"  警告 {item['file']}: {item['message']}")
        if not report["conformant"]:
            exit_code = 1
    return exit_code


def _cmd_info(args: argparse.Namespace) -> int:
    from .services import create_services

    services = create_services(args.data_dir)
    payload = {
        "version": __version__,
        "data_dir": str(services.paths.root),
        "sqlite": str(services.paths.sqlite_file),
        "vectors": str(services.paths.vectors_dir),
        "settings": services.settings.current.masked_dict(),
        "counts": services.counts(),
        "embedding": services.embedder.describe(),
        "providers": services.registry.status(),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    services.close()
    return 0


def _cmd_rebuild_kg(args: argparse.Namespace) -> int:
    from .services import create_services

    services = create_services(args.data_dir)
    hashes = None
    if getattr(args, "hashes", ""):
        hashes = [h.strip() for h in args.hashes.split(",") if h.strip()]
    rebuilt = services.rebuild_knowledge_graphs(hashes)
    print(f"知识图谱重建完成：{rebuilt} 篇论文")
    services.close()
    return 0


COMMANDS = {
    "serve": _cmd_serve,
    "ingest": _cmd_ingest,
    "doctor": _cmd_doctor,
    "okf-verify": _cmd_okf_verify,
    "info": _cmd_info,
    "rebuild-kg": _cmd_rebuild_kg,
}


def main(argv: Optional[List[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    command = args.command or "serve"
    if command not in COMMANDS:
        parser.print_help()
        return 2
    return COMMANDS[command](args)


if __name__ == "__main__":
    raise SystemExit(main())
