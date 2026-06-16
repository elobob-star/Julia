'''Command-line entry point.

  julia run             start the orchestrator (live)
  julia run --dry-run   full rehearsal with fakes; spends nothing
  julia status          print the task ledger
  julia backup DEST     copy durable state to DEST (one-command backup)
  julia restore SRC     restore durable state from a backup file
'''

from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

from .behavior.editor import LocalBehaviorEditor
from .config import Settings
from .gateway.console import ConsoleGateway
from .gateway.telegram import TelegramGateway
from .gh.client import FakeGitHubClient, HttpGitHubClient
from .jules.client import FakeJulesClient, HttpJulesClient
from .llm.provider import OpenAICompatibleModel, RuleBasedModel
from .orchestrator import Orchestrator
from .state import Store


def _build_behavior(settings: Settings):
    if settings.behaviors_path is None:
        return None
    return LocalBehaviorEditor(settings.behaviors_path)


def build_orchestrator(settings: Settings) -> Orchestrator:
    store = Store(settings.db_path())
    if settings.dry_run:
        return Orchestrator(
            settings, store, FakeJulesClient(), FakeGitHubClient(),
            RuleBasedModel(), ConsoleGateway(),
            behavior=_build_behavior(settings),
        )
    missing = settings.validate_live()
    if missing:
        raise SystemExit(
            'Missing credentials: ' + ', '.join(missing) + ' (or start with --dry-run).'
        )
    assert settings.jules_api_key is not None and settings.github_token is not None
    jules = HttpJulesClient(settings.jules_api_key.get_secret_value(), settings.jules_base_url)
    github = HttpGitHubClient(settings.github_token.get_secret_value(), settings.github_api_url)
    if settings.model_api_key and settings.model_base_url:
        model: OpenAICompatibleModel | RuleBasedModel = OpenAICompatibleModel(
            settings.model_base_url,
            settings.model_api_key.get_secret_value(),
            settings.model_name,
        )
    else:
        model = RuleBasedModel()
    if settings.telegram_bot_token and settings.telegram_chat_id:
        gateway: TelegramGateway | ConsoleGateway = TelegramGateway(
            settings.telegram_bot_token.get_secret_value(), settings.telegram_chat_id
        )
    else:
        gateway = ConsoleGateway()
    return Orchestrator(
        settings, store, jules, github, model, gateway, behavior=_build_behavior(settings)
    )


def main() -> None:
    parser = argparse.ArgumentParser(prog='julia', description=__doc__)
    subparsers = parser.add_subparsers(dest='command', required=True)
    run_parser = subparsers.add_parser('run', help='start the orchestrator')
    run_parser.add_argument(
        '--dry-run', action='store_true', help='rehearse with fakes; spends nothing'
    )
    run_parser.add_argument(
        '--behaviors',
        type=Path,
        help='local checkout of julia-behaviors; opens reviewable PRs back into it',
    )
    subparsers.add_parser('status', help='print the task ledger')
    backup_parser = subparsers.add_parser('backup', help='back up durable state')
    backup_parser.add_argument('dest')
    restore_parser = subparsers.add_parser('restore', help='restore state from a backup')
    restore_parser.add_argument('src')
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format='%(asctime)s %(name)s %(levelname)s %(message)s'
    )
    settings = Settings()

    if args.command == 'run':
        update: dict[str, object] = {}
        if args.dry_run:
            update['dry_run'] = True
        if getattr(args, 'behaviors', None) is not None:
            update['behaviors_path'] = args.behaviors
        if update:
            settings = settings.model_copy(update=update)
        orchestrator = build_orchestrator(settings)
        print('Julia is running. Type a request, or /help for commands.')
        try:
            asyncio.run(orchestrator.run())
        except KeyboardInterrupt:
            print('\nJulia stopped.')
    elif args.command == 'status':
        store = Store(settings.db_path())
        tasks = store.list_tasks()
        if not tasks:
            print('No tasks in the ledger yet.')
        for task in tasks:
            print(f'{task.id}  {task.state.value:<18} {task.repo}  {task.prompt[:60]}')
    elif args.command == 'backup':
        store = Store(settings.db_path())
        destination = store.backup(Path(args.dest))
        print(f'State backed up to {destination}')
    elif args.command == 'restore':
        source = Store(Path(args.src))
        source.backup(settings.db_path())
        print(f'State restored to {settings.db_path()}')


if __name__ == '__main__':
    main()
