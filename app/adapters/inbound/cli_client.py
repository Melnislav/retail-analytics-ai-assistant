"""Interactive CLI chat interface for the OpsFleet Retail Data Analysis Assistant."""

from __future__ import annotations

import asyncio
import os
from uuid import uuid4

import httpx
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table

console = Console()


class RetailAssistantCLI:
    """Async CLI client communicating with the FastAPI backend."""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.session_id = f"cli_{uuid4().hex[:8]}"
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

    def print_banner(self) -> None:
        app_title = os.getenv("APP_NAME", "OpsFleet Retail Data Analysis Assistant")
        console.print(
            Panel.fit(
                f"[bold cyan]{app_title} (CLI)[/bold cyan]\n"
                f"User: [green]{self.user_name}[/green] (`{self.user_id}`, role=`{self.user_role}`)\n"
                f"Session: [yellow]{self.session_id}[/yellow] | API: {self.base_url}\n"
                "Commands: [bold]/reports[/bold], [bold]/save <question>[/bold], "
                "[bold]/role <role> [brands][/bold], [bold]/exit[/bold]",
                border_style="cyan",
            )
        )

    async def run(self) -> None:
        self.print_banner()
        timeout = float(os.getenv("CLI_TIMEOUT_SECONDS", "60.0"))
        async with httpx.AsyncClient(timeout=timeout) as client:
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

                if text.lower() == "/reports":
                    try:
                        resp = await client.get(
                            f"{self.base_url}/api/v1/reports", headers=self.headers
                        )
                    except httpx.TimeoutException:
                        console.print(
                            "[bold yellow]Request timed out while fetching saved reports.[/bold yellow]"
                        )
                        continue
                    except httpx.ConnectError:
                        console.print(
                            f"[bold red]Could not connect to API at {self.base_url}.[/bold red]"
                        )
                        continue
                    except httpx.HTTPError as exc:
                        console.print(f"[bold red]Network error fetching reports:[/bold red] {exc}")
                        continue

                    if resp.status_code == 200:
                        reports = resp.json()
                        if not reports:
                            console.print("[dim]No saved reports found.[/dim]")
                        else:
                            tbl = Table(title="Saved Reports")
                            tbl.add_column("ID", style="cyan")
                            tbl.add_column("Title", style="bold")
                            tbl.add_column("Created At")
                            for r in reports:
                                tbl.add_row(r["report_id"][:8], r["title"], r["created_at"])
                            console.print(tbl)
                    else:
                        console.print(f"[red]Error {resp.status_code}: {resp.text}[/red]")
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
                            "[yellow]Possible causes:[/yellow]\n"
                            "• External AI provider is experiencing high latency or demand spikes.\n"
                            "• Server is executing a heavy analytical query.\n\n"
                            "[dim]Please retry your question or check the LLM provider configuration in .env.[/dim]",
                            title="[bold yellow]Request Timeout[/bold yellow]",
                            border_style="red",
                        )
                    )
                    continue
                except httpx.ConnectError:
                    console.print(
                        Panel(
                            f"[bold red]Could not connect to analytics backend ({self.base_url}).[/bold red]\n\n"
                            "Please ensure the [bold cyan]opsfleet-api[/bold cyan] container or server is running.",
                            title="[bold red]Connection Error[/bold red]",
                            border_style="red",
                        )
                    )
                    continue
                except httpx.HTTPError as exc:
                    console.print(
                        Panel(
                            f"[bold red]Network error contacting server:[/bold red] {exc}",
                            title="[bold red]Network Error[/bold red]",
                            border_style="red",
                        )
                    )
                    continue

                if resp.status_code != 200:
                    console.print(
                        f"[bold red]API Error ({resp.status_code}):[/bold red] {resp.text}"
                    )
                    continue

                data = resp.json()
                console.print(
                    Panel(Markdown(data["reply"]), title="Assistant", border_style="blue")
                )

                if data.get("sql_used"):
                    console.print(f"[dim]SQL: {data['sql_used']}[/dim]")
                if data.get("saved_report_id"):
                    console.print(f"[green]Saved Report ID: {data['saved_report_id']}[/green]")

                # Interactive confirmation prompt for high-stakes destructive ops
                pending = data.get("pending_confirmation")
                if pending:
                    approved = await asyncio.to_thread(
                        Confirm.ask,
                        f"[bold red]Confirm permanent deletion of {len(pending['target_report_ids'])} report(s)?[/bold red]",
                        default=False,
                    )
                    try:
                        conf_resp = await client.post(
                            f"{self.base_url}/api/v1/chat/confirm",
                            headers=self.headers,
                            json={
                                "session_id": self.session_id,
                                "token": pending["token"],
                                "approved": approved,
                            },
                        )
                    except httpx.TimeoutException:
                        console.print("[bold red]Timed out while confirming operation.[/bold red]")
                        continue
                    except httpx.ConnectError:
                        console.print(
                            f"[bold red]Could not connect to API at {self.base_url}.[/bold red]"
                        )
                        continue
                    except httpx.HTTPError as exc:
                        console.print(
                            f"[bold red]Network error during confirmation:[/bold red] {exc}"
                        )
                        continue

                    conf_data = conf_resp.json()
                    console.print(
                        Panel(
                            Markdown(conf_data["reply"]),
                            title="Confirmation Result",
                            border_style="yellow",
                        )
                    )


def main() -> None:
    api_port = os.getenv("API_PORT", "8000")
    api_host = os.getenv("API_HOST", "localhost")
    host_for_url = "localhost" if api_host in ("0.0.0.0", "") else api_host
    default_base_url = f"http://{host_for_url}:{api_port}"
    base_url = os.getenv("API_BASE_URL", default_base_url)
    cli = RetailAssistantCLI(base_url=base_url)
    asyncio.run(cli.run())


if __name__ == "__main__":
    main()
