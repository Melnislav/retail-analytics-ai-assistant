"""Interactive CLI chat interface for the Retail Data Analysis Assistant."""

from __future__ import annotations

import argparse
import asyncio
import os
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import httpx
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table

from app.domain.exceptions import (
    DomainError,
    ExternalServiceUnavailableError,
    QueryExecutionError,
    SecurityViolationError,
    SQLSafetyError,
    UnauthorizedDataAccessError,
)
from app.domain.models import ChatRequest, ConfirmationRequest, UserContext
from app.infrastructure.config import (
    get_settings,
    load_user_rbac_profiles,
    resolve_local_user_context,
)

if TYPE_CHECKING:
    from app.infrastructure.container import ApplicationContainer

console = Console()


class RetailAssistantCLI:
    """Async CLI client communicating with the FastAPI backend."""

    def __init__(
        self,
        base_url: str = "http://localhost:8000",
        user_key: str | None = None,
        standalone: bool = False,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.session_id = f"cli_{uuid4().hex[:8]}"
        self.standalone = standalone
        self.container: ApplicationContainer | None = None

        profiles = load_user_rbac_profiles()
        self.profile_key = user_key or "brand_manager_alice"
        if self.profile_key not in profiles:
            raise ValueError(f"Unknown local RBAC profile: {self.profile_key}")
        self._apply_profile(self.profile_key)

    @property
    def headers(self) -> dict[str, str]:
        """No caller-controlled identity headers are sent to the prototype HTTP API."""
        return {}

    @property
    def user_context(self) -> UserContext:
        return resolve_local_user_context(self.profile_key)

    def _apply_profile(self, profile_key: str) -> None:
        profile = resolve_local_user_context(profile_key)
        self.profile_key = profile_key
        self.user_id = profile.user_id
        self.user_name = profile.display_name
        self.user_role = profile.role.value
        self.allowed_brands = ",".join(profile.allowed_brands)

    def print_banner(self) -> None:
        app_title = os.getenv("APP_NAME", "Retail Data Analysis Assistant")
        mode_str = "Direct In-Process" if self.standalone else f"HTTP: {self.base_url}"
        console.print(
            Panel.fit(
                f"[bold cyan]{app_title} (CLI)[/bold cyan]\n"
                f"User: [green]{self.user_name}[/green] (`{self.user_id}`, role=`{self.user_role}`)\n"
                f"Session: [yellow]{self.session_id}[/yellow] | Mode: [magenta]{mode_str}[/magenta]\n"
                "Commands: [bold]/help[/bold], [bold]/users[/bold], [bold]/user <id>[/bold], [bold]/clear[/bold], "
                "[bold]/reports[/bold], [bold]/report <id>[/bold], [bold]/save <question>[/bold], "
                "[bold]/metrics[/bold], [bold]/trace [id][/bold], [bold]/exit[/bold]",
                border_style="cyan",
            )
        )

    def print_help(self) -> None:
        console.print(
            Panel(
                "[bold yellow]Sample Retail Assistant Questions (from Assessment Brief):[/bold yellow]\n\n"
                "1. [bold cyan]Customer Churn:[/bold cyan]\n"
                '   • "Why did our churn rate spike last month?"\n'
                '   • "Calculate churn defining churn as no purchase in 60 days"\n\n'
                "2. [bold cyan]Product & Revenue Performance:[/bold cyan]\n"
                '   • "Compare performance of product Calvin Klein and Tommy Hilfiger"\n'
                '   • "What is our GMV and Net Revenue by product category?"\n'
                '   • "Show repeat customer rate and repeat buyers by category"\n\n'
                "3. [bold cyan]Multi-Brand & Demographics (Zero-PII & Brand Scoped):[/bold cyan]\n"
                '   • "Why are users in state California underspending compared to New York?"\n\n'
                "4. [bold cyan]High-Stakes Destructive Actions:[/bold cyan]\n"
                '   • "/save What are our top 5 revenue categories?"\n'
                '   • "Delete all reports made in this conversation"\n\n'
                "[bold yellow]Available Commands:[/bold yellow]\n"
                "• [bold]/users[/bold] - List available RBAC profiles from configs/users_rbac.yaml\n"
                "• [bold]/user <name>[/bold] - Switch persona (e.g. /user ceo or /user alice)\n"
                "• [bold]/clear[/bold] - Reset session conversation memory\n"
                "• [bold]/reports[/bold] - List your saved reports\n"
                "• [bold]/report <id>[/bold] - View a full saved report (question, SQL, and answer)\n"
                "• [bold]/save <query>[/bold] - Ask question and save executive report\n"
                "• [bold]/metrics[/bold] - Show local agent counters and latency percentiles\n"
                "• [bold]/trace [id][/bold] - Inspect recent execution traces or a specific trace ID\n"
                "• [bold]/exit[/bold] - Exit CLI",
                title="[bold green]Retail Assistant Help & Sample Queries[/bold green]",
                border_style="green",
            )
        )

    @staticmethod
    def _render_standalone_error(exc: Exception) -> None:
        if isinstance(exc, (SecurityViolationError, SQLSafetyError, UnauthorizedDataAccessError)):
            console.print(
                Panel(
                    f"[bold red]{exc.message}[/bold red]",
                    title="[bold red]Security / Policy Refusal[/bold red]",
                    border_style="red",
                )
            )
        elif isinstance(exc, QueryExecutionError):
            console.print(
                Panel(
                    f"[bold yellow]{exc.message}[/bold yellow]",
                    title="[bold yellow]Query Execution Error[/bold yellow]",
                    border_style="yellow",
                )
            )
        elif isinstance(exc, ExternalServiceUnavailableError):
            console.print(
                Panel(
                    f"[bold red]{exc.message}[/bold red]",
                    title="[bold red]Service Unavailable[/bold red]",
                    border_style="red",
                )
            )
        elif isinstance(exc, DomainError):
            console.print(
                Panel(
                    f"[bold yellow]{exc.message}[/bold yellow]",
                    title="[bold yellow]Request Error[/bold yellow]",
                    border_style="yellow",
                )
            )
        else:
            console.print(
                Panel(
                    f"[bold red]Unexpected error:[/bold red] {exc}",
                    title="[bold red]Unexpected Error[/bold red]",
                    border_style="red",
                )
            )

    async def _send_chat(
        self, client: httpx.AsyncClient | None, text: str, save_flag: bool
    ) -> dict[str, Any] | None:
        if self.standalone and self.container:
            req = ChatRequest(
                session_id=self.session_id,
                message=text,
                save_report=save_flag,
            )
            try:
                res = await self.container.chat_orchestrator.handle_message(req, self.user_context)
            except Exception as exc:
                self._render_standalone_error(exc)
                return None
            return {
                "reply": res.reply,
                "sql_used": res.sql_used,
                "saved_report_id": res.saved_report_id,
                "pending_confirmation": (
                    res.pending_confirmation.model_dump() if res.pending_confirmation else None
                ),
            }

        if client is None:
            return None

        try:
            resp = await client.post(
                f"{self.base_url}/api/v1/chat",
                headers=self.headers,
                json={
                    "session_id": self.session_id,
                    "message": text,
                    "save_report": save_flag,
                },
            )
        except httpx.TimeoutException:
            console.print(
                Panel(
                    "[bold red]Request timed out waiting for analytical assistant response.[/bold red]\n\n"
                    "[dim]Please retry your question or check the LLM provider configuration in .env.[/dim]",
                    title="[bold yellow]Request Timeout[/bold yellow]",
                    border_style="red",
                )
            )
            return None
        except httpx.ConnectError:
            console.print(
                Panel(
                    f"[bold red]Could not connect to analytics backend ({self.base_url}).[/bold red]\n\n"
                    "Tip: run with [bold green]--standalone[/bold green] to evaluate in-process without an HTTP server.",
                    title="[bold red]Connection Error[/bold red]",
                    border_style="red",
                )
            )
            return None
        except httpx.HTTPError as exc:
            console.print(
                Panel(
                    f"[bold red]Network error contacting server:[/bold red] {exc}",
                    title="[bold red]Network Error[/bold red]",
                    border_style="red",
                )
            )
            return None

        if resp.status_code != 200:
            console.print(f"[bold red]API Error ({resp.status_code}):[/bold red] {resp.text}")
            return None
        return resp.json()

    async def _confirm_action(
        self, client: httpx.AsyncClient | None, token: str, approved: bool
    ) -> dict[str, Any] | None:
        if self.standalone and self.container:
            req = ConfirmationRequest(
                session_id=self.session_id,
                token=token,
                approved=approved,
            )
            try:
                res = await self.container.chat_orchestrator.confirm_action(req, self.user_context)
            except Exception as exc:
                self._render_standalone_error(exc)
                return None
            return {"reply": res.reply}

        if client is None:
            return None

        try:
            conf_resp = await client.post(
                f"{self.base_url}/api/v1/chat/confirm",
                headers=self.headers,
                json={
                    "session_id": self.session_id,
                    "token": token,
                    "approved": approved,
                },
            )
            return conf_resp.json()
        except Exception as exc:
            console.print(f"[bold red]Error confirming operation:[/bold red] {exc}")
            return None

    async def _list_reports(self, client: httpx.AsyncClient | None) -> None:
        reports_list: list[dict[str, Any]] = []
        if self.standalone and self.container:
            try:
                reports = await self.container.report_service.list_user_reports(self.user_context)
            except Exception as exc:
                self._render_standalone_error(exc)
                return
            reports_list = [r.model_dump() for r in reports]
        elif client:
            try:
                resp = await client.get(f"{self.base_url}/api/v1/reports", headers=self.headers)
                if resp.status_code == 200:
                    reports_list = resp.json()
                else:
                    console.print(f"[red]Error {resp.status_code}: {resp.text}[/red]")
                    return
            except Exception as exc:
                console.print(f"[bold red]Error fetching reports:[/bold red] {exc}")
                return

        if not reports_list:
            console.print("[dim]No saved reports found.[/dim]")
            return

        tbl = Table(title=f"Saved Reports ({self.user_name})")
        tbl.add_column("ID", style="cyan")
        tbl.add_column("Title", style="bold")
        tbl.add_column("Created At")
        for r in reports_list:
            tbl.add_row(
                str(r["report_id"])[:8], str(r.get("title", "")), str(r.get("created_at", ""))
            )
        console.print(tbl)
        console.print("[dim]Tip: run /report <id> to inspect question, SQL, and answer.[/dim]")

    async def _view_report(self, client: httpx.AsyncClient | None, report_id: str) -> None:
        normalized_id = report_id.strip()
        if not normalized_id:
            console.print("[yellow]Usage: /report <id>[/yellow]")
            return

        report_data: dict[str, Any] | None = None
        if self.standalone and self.container:
            try:
                report = await self.container.report_service.get_user_report(
                    self.user_context, normalized_id
                )
            except Exception as exc:
                self._render_standalone_error(exc)
                return
            report_data = report.model_dump(mode="json") if report else None
        elif client:
            try:
                resp = await client.get(
                    f"{self.base_url}/api/v1/reports/{normalized_id}",
                    headers=self.headers,
                )
                if resp.status_code == 200:
                    report_data = resp.json()
                elif resp.status_code == 404:
                    report_data = None
                else:
                    console.print(f"[red]Error {resp.status_code}: {resp.text}[/red]")
                    return
            except Exception as exc:
                console.print(f"[bold red]Error fetching report:[/bold red] {exc}")
                return

        if not report_data:
            console.print(
                f"[yellow]Report '{normalized_id}' not found for user '{self.user_id}'.[/yellow]"
            )
            return

        question = report_data.get("question") or "(not recorded)"
        sql_used = report_data.get("sql_used") or "(none)"
        header_md = (
            f"**ID:** `{report_data['report_id']}`  \n"
            f"**Created:** {report_data.get('created_at', '')}  \n"
            f"**Question:** {question}\n\n"
            f"**SQL:**\n```sql\n{sql_used}\n```\n\n"
            f"---\n\n{report_data.get('content_markdown', '')}"
        )
        console.print(
            Panel(
                Markdown(header_md),
                title=f"Saved Report: {report_data.get('title', 'Untitled')}",
                border_style="cyan",
            )
        )

    async def _show_metrics(self, client: httpx.AsyncClient | None) -> None:
        snapshot: dict[str, Any] | None = None
        if self.standalone and self.container:
            snapshot = self.container.observability.get_metrics_snapshot()
        elif client:
            try:
                resp = await client.get(f"{self.base_url}/api/v1/metrics", headers=self.headers)
                if resp.status_code == 200:
                    snapshot = resp.json()
                else:
                    console.print(f"[red]Error {resp.status_code}: {resp.text}[/red]")
                    return
            except Exception as exc:
                console.print(f"[bold red]Error fetching metrics:[/bold red] {exc}")
                return

        if not snapshot:
            console.print("[dim]No metrics available.[/dim]")
            return

        tbl = Table(title="Agent Observability Metrics")
        tbl.add_column("Metric", style="cyan")
        tbl.add_column("Value", style="bold")
        for key, val in sorted((snapshot.get("counters") or {}).items()):
            tbl.add_row(str(key), str(val))
        lat = snapshot.get("latency_ms") or {}
        if lat:
            tbl.add_row(
                "latency_ms (p50 / p95 / p99)",
                f"{lat.get('p50', 0)} / {lat.get('p95', 0)} / {lat.get('p99', 0)} (n={lat.get('count', 0)})",
            )
        if snapshot.get("trace_log_path"):
            tbl.add_row("trace_log_path", str(snapshot["trace_log_path"]))
        console.print(tbl)

    async def _show_trace(
        self, client: httpx.AsyncClient | None, trace_id: str | None = None
    ) -> None:
        scoped_user = None if self.user_role in {"ceo", "admin"} else self.user_id
        if trace_id:
            record_data: dict[str, Any] | None = None
            if self.standalone and self.container:
                rec = self.container.observability.get_trace(trace_id, user_id=scoped_user)
                record_data = rec.model_dump(mode="json") if rec else None
            elif client:
                try:
                    resp = await client.get(
                        f"{self.base_url}/api/v1/traces/{trace_id}", headers=self.headers
                    )
                    if resp.status_code == 200:
                        record_data = resp.json()
                    elif resp.status_code != 404:
                        console.print(f"[red]Error {resp.status_code}: {resp.text}[/red]")
                        return
                except Exception as exc:
                    console.print(f"[bold red]Error fetching trace:[/bold red] {exc}")
                    return

            if not record_data:
                console.print(f"[yellow]Trace '{trace_id}' not found.[/yellow]")
                return

            detail_md = (
                f"**Trace ID:** `{record_data['trace_id']}` | **User:** `{record_data['user_id']}` | "
                f"**Session:** `{record_data['session_id']}`  \n"
                f"**Intent:** `{record_data.get('intent_classified')}` | **AST Valid:** `{record_data.get('ast_valid')}` | "
                f"**Fallback Used:** `{record_data.get('fallback_used')}`  \n"
                f"**Latency:** {record_data.get('latency_ms', 0)} ms | "
                f"**LLM Calls:** {record_data.get('llm_calls_count', 0)} | "
                f"**Cost:** ${record_data.get('total_cost_usd', 0):.6f} | "
                f"**BQ Bytes:** {record_data.get('bq_bytes_billed', 0)}  \n"
                f"**Error Type:** `{record_data.get('error_type')}`  \n"
                f"**Prompt (redacted):** {record_data.get('pii_redacted_prompt', '')}\n\n"
                f"**SQL:**\n```sql\n{record_data.get('generated_sql') or '(none)'}\n```"
            )
            console.print(
                Panel(Markdown(detail_md), title="Execution Trace Detail", border_style="magenta")
            )
            return

        traces_list: list[dict[str, Any]] = []
        if self.standalone and self.container:
            recs = self.container.observability.get_recent_traces(limit=10, user_id=scoped_user)
            traces_list = [r.model_dump(mode="json") for r in recs]
        elif client:
            try:
                resp = await client.get(f"{self.base_url}/api/v1/traces", headers=self.headers)
                if resp.status_code == 200:
                    traces_list = resp.json()
                else:
                    console.print(f"[red]Error {resp.status_code}: {resp.text}[/red]")
                    return
            except Exception as exc:
                console.print(f"[bold red]Error fetching traces:[/bold red] {exc}")
                return

        if not traces_list:
            console.print("[dim]No execution traces recorded yet.[/dim]")
            return

        tbl = Table(title="Recent Execution Traces")
        tbl.add_column("Trace ID", style="cyan")
        tbl.add_column("Intent")
        tbl.add_column("LLM")
        tbl.add_column("Cost ($)")
        tbl.add_column("Latency (ms)")
        tbl.add_column("Error")
        for tr in traces_list:
            tbl.add_row(
                str(tr.get("trace_id", "")),
                str(tr.get("intent_classified", "")),
                str(tr.get("llm_calls_count", 0)),
                f"{float(tr.get('total_cost_usd', 0)):.5f}",
                str(tr.get("latency_ms", 0)),
                str(tr.get("error_type") or "-"),
            )
        console.print(tbl)
        console.print(
            "[dim]Tip: run /trace <trace_id> to inspect prompt, SQL, and failure reason.[/dim]"
        )

    async def run(self) -> None:
        if self.standalone:
            from app.infrastructure.container import ApplicationContainer

            settings = get_settings()
            self.container = ApplicationContainer(settings)
            await self.container.startup()

        self.print_banner()
        timeout = float(os.getenv("CLI_TIMEOUT_SECONDS", "60.0"))
        client_ctx = httpx.AsyncClient(timeout=timeout) if not self.standalone else None

        try:
            while True:
                try:
                    user_input = await asyncio.to_thread(
                        Prompt.ask, f"\n[bold green]{self.user_id}[/bold green]"
                    )
                except (EOFError, KeyboardInterrupt):
                    console.print("\n[yellow]Exiting CLI.[/yellow]")
                    break

                text = user_input.strip()
                if not text:
                    continue
                if text.lower() in {"/exit", "exit", "quit"}:
                    break

                if text.lower() == "/help":
                    self.print_help()
                    continue

                if text.lower() == "/clear":
                    self.session_id = f"cli_{uuid4().hex[:8]}"
                    console.print(
                        f"[yellow]Session memory cleared. New session: `{self.session_id}`[/yellow]"
                    )
                    continue

                if text.lower() == "/users":
                    profiles = load_user_rbac_profiles()
                    tbl = Table(title="Available User RBAC Profiles")
                    tbl.add_column("Key", style="bold cyan")
                    tbl.add_column("Display Name")
                    tbl.add_column("Role", style="green")
                    tbl.add_column("Allowed Brands", style="yellow")
                    for k, v in profiles.items():
                        brands = ", ".join(v.get("allowed_brands", [])) or "ALL (Unrestricted)"
                        tbl.add_row(k, v.get("display_name", ""), v.get("role", ""), brands)
                    console.print(tbl)
                    continue

                if text.lower().startswith("/user "):
                    target_key = text.split(maxsplit=1)[1].strip()
                    profiles = load_user_rbac_profiles()
                    match = profiles.get(target_key)
                    if not match:
                        for k, v in profiles.items():
                            if (
                                target_key.lower() in k.lower()
                                or target_key.lower() in v["display_name"].lower()
                            ):
                                match = v
                                break
                    if match:
                        matched_key = next(key for key, value in profiles.items() if value is match)
                        self._apply_profile(matched_key)
                        console.print(
                            f"[green]Switched active user to [bold]{self.user_name}[/bold] "
                            f"(`{self.user_id}`, role=`{self.user_role}`, brands=`{self.allowed_brands or 'ALL'}`)[/green]"
                        )
                    else:
                        console.print(
                            f"[red]Unknown user key '{target_key}'. Type /users to see list.[/red]"
                        )
                    continue

                if text.lower() == "/reports":
                    await self._list_reports(client_ctx)
                    continue

                if text.lower() == "/report":
                    console.print("[yellow]Usage: /report <id>[/yellow]")
                    continue

                if text.lower().startswith("/report "):
                    target_id = text.split(maxsplit=1)[1].strip()
                    await self._view_report(client_ctx, target_id)
                    continue

                if text.lower() == "/metrics":
                    await self._show_metrics(client_ctx)
                    continue

                if text.lower() == "/trace":
                    await self._show_trace(client_ctx)
                    continue

                if text.lower().startswith("/trace "):
                    target_trace_id = text.split(maxsplit=1)[1].strip()
                    await self._show_trace(client_ctx, target_trace_id)
                    continue

                if text.lower().startswith("/role "):
                    console.print(
                        "[yellow]Role overrides are disabled. Use /users then /user <profile> "
                        "to select a configured local profile.[/yellow]"
                    )
                    continue

                save_flag = False
                if text.lower().startswith("/save "):
                    save_flag = True
                    text = text[len("/save ") :].strip()

                data = await self._send_chat(client_ctx, text, save_flag)
                if not data:
                    continue

                console.print(
                    Panel(Markdown(data["reply"]), title="Assistant", border_style="blue")
                )

                if data.get("sql_used"):
                    console.print(f"[dim]SQL: {data['sql_used']}[/dim]")
                if data.get("saved_report_id"):
                    console.print(f"[green]Saved Report ID: {data['saved_report_id']}[/green]")

                pending = data.get("pending_confirmation")
                if pending:
                    hash_hint = (
                        f" [hash: {pending['payload_hash'][:12]}]"
                        if pending.get("payload_hash")
                        else ""
                    )
                    approved = await asyncio.to_thread(
                        Confirm.ask,
                        f"[bold red]Confirm permanent deletion of {len(pending['target_report_ids'])} report(s){hash_hint}?[/bold red]",
                        default=False,
                    )
                    conf_data = await self._confirm_action(client_ctx, pending["token"], approved)
                    if conf_data:
                        console.print(
                            Panel(
                                Markdown(conf_data["reply"]),
                                title="Confirmation Result",
                                border_style="yellow",
                            )
                        )
        finally:
            if client_ctx:
                await client_ctx.aclose()
            if self.standalone and self.container:
                await self.container.shutdown()


def main() -> None:
    parser = argparse.ArgumentParser(description="Retail Assistant CLI")
    parser.add_argument(
        "--standalone",
        action="store_true",
        help="Run in-process without an external HTTP server",
    )
    parser.add_argument(
        "--user",
        type=str,
        default=None,
        help="Initial user key from configs/users_rbac.yaml (e.g. ceo, brand_manager_alice)",
    )
    parser.add_argument(
        "--api-url",
        type=str,
        default=None,
        help="FastAPI base URL (defaults to http://localhost:8000)",
    )
    args = parser.parse_args()

    api_port = os.getenv("API_PORT", "8000")
    api_host = os.getenv("API_HOST", "localhost")
    host_for_url = "localhost" if api_host in ("0.0.0.0", "") else api_host
    default_base_url = f"http://{host_for_url}:{api_port}"
    base_url = args.api_url or os.getenv("API_BASE_URL", default_base_url)

    cli = RetailAssistantCLI(
        base_url=base_url,
        user_key=args.user,
        standalone=args.standalone,
    )
    asyncio.run(cli.run())


if __name__ == "__main__":
    main()
