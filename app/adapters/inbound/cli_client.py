"""Interactive CLI chat interface for the OpsFleet Retail Data Analysis Assistant."""

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

from app.domain.models import ChatRequest, ConfirmationRequest, UserContext, UserRole
from app.infrastructure.config import get_settings, load_user_rbac_profiles

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
        selected_profile = None
        if user_key and user_key in profiles:
            selected_profile = profiles[user_key]
        elif not user_key and "brand_manager_alice" in profiles:
            selected_profile = profiles["brand_manager_alice"]

        if selected_profile:
            self.user_id = selected_profile["user_id"]
            self.user_name = selected_profile["display_name"]
            self.user_role = selected_profile["role"]
            self.allowed_brands = ",".join(selected_profile.get("allowed_brands", []))
        else:
            self.user_id = os.getenv("CLI_USER_ID", "manager_alice")
            self.user_name = os.getenv("CLI_USER_NAME", "Alice (Brand Manager)")
            self.user_role = os.getenv("CLI_USER_ROLE", "brand_manager")
            self.allowed_brands = os.getenv("CLI_ALLOWED_BRANDS", "")

    @property
    def headers(self) -> dict[str, str]:
        return {
            "X-User-Id": self.user_id,
            "X-User-Name": self.user_name,
            "X-User-Role": self.user_role,
            "X-Allowed-Brands": self.allowed_brands,
        }

    @property
    def user_context(self) -> UserContext:
        brands = [b.strip() for b in self.allowed_brands.split(",") if b.strip()]
        role = UserRole.CEO if self.user_role.lower() == "ceo" else UserRole.BRAND_MANAGER
        return UserContext(
            user_id=self.user_id,
            display_name=self.user_name,
            role=role,
            allowed_brands=brands,
        )

    def print_banner(self) -> None:
        app_title = os.getenv("APP_NAME", "OpsFleet Retail Data Analysis Assistant")
        mode_str = "Direct In-Process" if self.standalone else f"HTTP: {self.base_url}"
        console.print(
            Panel.fit(
                f"[bold cyan]{app_title} (CLI)[/bold cyan]\n"
                f"User: [green]{self.user_name}[/green] (`{self.user_id}`, role=`{self.user_role}`)\n"
                f"Session: [yellow]{self.session_id}[/yellow] | Mode: [magenta]{mode_str}[/magenta]\n"
                "Commands: [bold]/help[/bold], [bold]/users[/bold], [bold]/user <id>[/bold], [bold]/clear[/bold], "
                "[bold]/reports[/bold], [bold]/save <question>[/bold], [bold]/exit[/bold]",
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
                "• [bold]/role <role> [brands][/bold] - Custom role switch (e.g. /role brand_manager Nike)\n"
                "• [bold]/clear[/bold] - Reset session conversation memory\n"
                "• [bold]/reports[/bold] - List your saved reports\n"
                "• [bold]/save <query>[/bold] - Ask question and save executive report\n"
                "• [bold]/exit[/bold] - Exit CLI",
                title="[bold green]Retail Assistant Help & Sample Queries[/bold green]",
                border_style="green",
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
            res = await self.container.chat_orchestrator.handle_message(req, self.user_context)
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
            res = await self.container.chat_orchestrator.confirm_action(req, self.user_context)
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
            reports = await self.container.report_repo.list_by_owner(self.user_id)
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
                        self.user_id = match["user_id"]
                        self.user_name = match["display_name"]
                        self.user_role = match["role"]
                        self.allowed_brands = ",".join(match.get("allowed_brands", []))
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

                if text.lower().startswith("/role "):
                    parts = text.split(maxsplit=2)
                    self.user_role = parts[1]
                    self.allowed_brands = parts[2] if len(parts) > 2 else ""
                    console.print(
                        f"[yellow]Switched role to `{self.user_role}` "
                        f"(brands: `{self.allowed_brands or 'NONE'}`)[/yellow]"
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
                    approved = await asyncio.to_thread(
                        Confirm.ask,
                        f"[bold red]Confirm permanent deletion of {len(pending['target_report_ids'])} report(s)?[/bold red]",
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
    parser = argparse.ArgumentParser(description="OpsFleet Retail Assistant CLI")
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
