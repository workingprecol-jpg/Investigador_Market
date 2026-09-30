import argparse
import json
import logging
import signal
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import load_config
from .exchange import BinanceDemoClient
from .report import build_report, read_state, write_report
from .storage import ProcessLock, Store


def main():
    parser = argparse.ArgumentParser(description="Final Boss: futuros Binance Demo, 100 USDT, 5x")
    parser.add_argument("command", choices=("scan", "scan-dashboard", "scan-status", "run", "status", "report", "health", "probe", "stop", "resume", "research", "daily-review", "rollback", "dashboard"))
    parser.add_argument("--config", default="config.toml")
    parser.add_argument("--mode", choices=("paper", "demo"))
    parser.add_argument("--state", help="SQLite alternativo; predeterminado data/<mode>.sqlite3")
    parser.add_argument("--once", action="store_true", help="Un solo ciclo")
    parser.add_argument("--output", help="Ruta .json o .html para report")
    parser.add_argument("--date", help="Dia YYYY-MM-DD a revisar, hora de Colombia")
    parser.add_argument("--preview", action="store_true", help="Informe parcial; nunca inicia experimentos ni aplica propuestas")
    parser.add_argument("--port", type=int, help="Puerto local del dashboard; paper 8765, demo 8766")
    args = parser.parse_args()
    if args.command in ("scan", "scan-dashboard", "scan-status"):
        from .scanner import run_scanner, scanner_snapshot
        scan_path = Path(args.output or "data/scanner.json")
        if args.command == "scan":
            return run_scanner(scan_path, once=args.once)
        if args.command == "scan-status":
            # ASCII JSON remains valid even on Windows consoles with legacy encodings.
            print(json.dumps(scanner_snapshot(scan_path), ensure_ascii=True, indent=2))
            return 0
        from .dashboard import DashboardServer
        port = args.port if args.port is not None else 8765
        if not 1 <= port <= 65535:
            raise ValueError("Puerto local fuera de rango")
        server = DashboardServer(scan_path, mode="analysis", port=port,
                                 html_path=Path(__file__).with_name("scanner.html"))
        try:
            server.serve_forever(poll_interval=.5)
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
        return 0
    # This application now has an analysis-only policy, including containers
    # and alternate working directories. Removing a marker cannot enable orders.
    if args.command == "run":
        print("Trading deshabilitado por solicitud del usuario. Usa: python -m bot scan", file=sys.stderr)
        return 2
    config = load_config(args.config, args.mode)
    path = Path(args.state or f"data/{config.mode}.sqlite3")
    if args.command == "dashboard":
        from .dashboard import serve_dashboard
        port = args.port if args.port is not None else (8765 if config.mode == "paper" else 8766)
        if not 1 <= port <= 65535:
            raise ValueError("Puerto local fuera de rango")
        serve_dashboard(path, config.mode, port, config.interval)
        return 0
    if args.command == "daily-review":
        from .daily import run_daily_review
        report = run_daily_review(config, path, args.date, preview=args.preview)
        if args.output:
            target = Path(args.output)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
            print(str(target.resolve()))
        else:
            print(json.dumps({"date":report["date"], "status":report["status"], "decision":report.get("decision"),
                              "specialists":len(report.get("specialists",[]))},ensure_ascii=False))
        return 0
    if args.command == "research":
        from .research import run_research
        result = run_research(config, BinanceDemoClient())
        destination = Path(args.output or "reports/research.json")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False), encoding="utf-8")
        print(str(destination.resolve()))
        return 0
    if args.command == "probe":
        client = BinanceDemoClient()
        result = []
        for symbol in config.symbols:
            info = client.exchange_info(symbol)
            snap = client.snapshot(symbol, config.interval, 240)
            premium = client.premium_index(symbol)
            result.append(dict(symbol=symbol, status=info["status"], price=snap.price,
                               closed_candles=len(snap.candles), funding_rate=premium["lastFundingRate"],
                               source="Binance Demo USD-M"))
        print(json.dumps(result, indent=2))
        return 0
    if args.command == "stop":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.with_suffix(".STOP").write_text("User requested halt", encoding="utf-8")
        print("Parada solicitada. El proceso cerrara las posiciones registradas en el siguiente ciclo con conexion.")
        return 0
    if args.command in ("status", "report", "health"):
        state = read_state(path)
        if args.command == "health":
            ok = state["healthy"] and time.time()-state["heartbeat"] < max(180, config.poll_seconds*4)
            print("healthy" if ok else "unhealthy")
            return 0 if ok else 1
        report = build_report(state)
        if args.command == "report" and args.output:
            write_report(report, args.output)
            print(str(Path(args.output).resolve()))
        else:
            print(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False))
        return 0
    with ProcessLock(path.with_suffix(".lock")):
        store = Store(path)
        try:
            if args.command == "rollback":
                from .governance import rollback
                state = store.load()
                if not state:
                    raise ValueError("No existe una estrategia persistida")
                rollback(state, config, time.time())
                store.save(state, "manual_rollback", state["strategy"]["history"][-1])
                print("Version anterior restaurada; reinicia el proceso para cargarla.")
                return 0
            if args.command == "resume":
                state = store.load()
                if not state or state["positions"] or state["pending"]:
                    raise ValueError("Resume exige estado sin posiciones ni ordenes pendientes; detener proceso antes")
                if state["equity"] <= config.initial_equity-config.max_total_loss_usdt:
                    raise ValueError("No se puede reanudar debajo del limite total")
                state["halt_reason"] = None
                state["healthy"] = False
                store.save(state, "manual_resume", {})
                path.with_suffix(".STOP").unlink(missing_ok=True)
                print("Reanudado; los limites de riesgo se verificaran al reiniciar run.")
                return 0
            return run(config, store, args.once, args.config)
        finally:
            store.close()


def run(config, store, once, config_path="config.toml"):
    from .engine import Coordinator
    from .daily import DailyReviewRunner
    logger = logging.getLogger("finalboss")
    logger.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    handlers = [RotatingFileHandler(store.path.with_suffix(".log"), maxBytes=2_000_000, backupCount=5, encoding="utf-8")]
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler())
    for handler in handlers:
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    coordinator = Coordinator(config, store)
    daily_runner = DailyReviewRunner(config, store.path, config_path)
    stopping = False
    def stop_process(*_):
        nonlocal stopping
        stopping = True
    signal.signal(signal.SIGINT, stop_process)
    signal.signal(signal.SIGTERM, stop_process)
    failure_count = 0
    daily_activity_token = None
    daily_activity_date = None
    logger.info("Inicio mode=%s symbols=%s leverage=5 capital=%.2f", config.mode, ",".join(config.symbols), config.initial_equity)
    while not stopping:
        activity_token = coordinator.activity.start("coordinator", "Datos de mercado, posiciones y señales")
        try:
            try:
                daily_report = daily_runner.poll(coordinator.state)
                coordinator.daily_maintenance(daily_report)
                coordinator.state["daily_schedule"] = daily_runner.status()
                schedule = coordinator.state["daily_schedule"]
                if schedule["running"]:
                    if daily_activity_token is None:
                        daily_activity_date = schedule["review_date"]
                        daily_activity_token = coordinator.activity.start(
                            "research_validation", "Revisión del historial: "+str(daily_activity_date))
                else:
                    if daily_activity_token is not None:
                        coordinator.activity.finish("research_validation", daily_activity_token,
                            result={"date": daily_activity_date,
                                    "decision": daily_report.get("decision") if daily_report else "Revisión finalizada"},
                            error=RuntimeError("Fallo del revisor") if schedule.get("error") else None)
                        daily_activity_token = None
                    if not schedule.get("error"):
                        coordinator.activity.idle("research_validation", "Esperando la revisión diaria",
                                                  schedule.get("next_review_at"))
            except Exception as exc:
                coordinator.state["daily_schedule"] = {**daily_runner.status(), "error": type(exc).__name__}
                if daily_activity_token is not None:
                    coordinator.activity.finish("research_validation", daily_activity_token, error=exc)
                    daily_activity_token = None
                logger.error("Revision diaria pendiente (%s); gestion de posiciones continua", type(exc).__name__)
            state = coordinator.cycle()
            coordinator.activity.heartbeat(state["heartbeat"])
            coordinator.activity.finish("coordinator", activity_token,
                result={"reason": "Ciclo completado" if state["healthy"] else "Ciclo con incidencias; revisar estado",
                        "observations": len(state.get("last_analysis", {}))})
            failure_count = 0
            logger.info("equity=%.4f positions=%s trades=%s pause=%s halt=%s", state["equity"],
                        len(state["positions"]), len(state["trades"]), state["daily_paused"], state["halt_reason"])
            report_name = store.path.stem
            write_report(build_report(state), f"reports/{report_name}.json")
            write_report(build_report(state), f"reports/{report_name}.html")
            if once:
                return 0 if state["healthy"] else 1
        except Exception as exc:
            coordinator.activity.finish("coordinator", activity_token, error=exc)
            failure_count += 1
            coordinator.state["healthy"] = False
            # Do not include arbitrary exception text/URLs that may contain credentials.
            coordinator.save("cycle_error", {"type": type(exc).__name__, "consecutive": failure_count})
            logger.error("Ciclo fallido (%s); nuevas entradas suspendidas durante el error", type(exc).__name__)
            if once:
                return 1
        wait = min(60, config.poll_seconds*(2**min(failure_count, 2)))
        if not failure_count:
            coordinator.activity.idle("coordinator", "Esperando el próximo ciclo", time.time()+wait)
        deadline = time.monotonic()+wait
        while not stopping and time.monotonic() < deadline:
            if store.path.with_suffix(".RESTART").exists():
                store.path.with_suffix(".RESTART").unlink(missing_ok=True)
                stopping = True
                break
            time.sleep(min(1, max(0, deadline-time.monotonic())))
    coordinator.activity.idle("coordinator", "Proceso detenido")
    if daily_activity_token is not None:
        coordinator.activity.finish("research_validation", daily_activity_token,
                                    error=RuntimeError("Supervisión del revisor interrumpida"))
    logger.info("Proceso detenido. Las posiciones Demo conservan su stop en Binance; STOP solicita cerrar primero.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, RuntimeError, OSError) as exc:
        print(f"No se pudo completar: {type(exc).__name__}. Revisa configuracion, conexion y estado local.", file=sys.stderr)
        sys.exit(2)
