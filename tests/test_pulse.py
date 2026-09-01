import importlib.machinery
import importlib.util
import fcntl
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import nullcontext, redirect_stdout
from datetime import datetime, timedelta, timezone
from unittest import mock
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "pulse"
loader = importlib.machinery.SourceFileLoader("project_pulse", str(SCRIPT))
spec = importlib.util.spec_from_loader(loader.name, loader)
pulse = importlib.util.module_from_spec(spec)
assert spec and spec.loader
sys.modules[loader.name] = pulse
spec.loader.exec_module(pulse)


def command(*args, cwd):
    return subprocess.run(args, cwd=cwd, check=True, text=True, capture_output=True)


def make_repo(path: Path, package: bool = False) -> None:
    path.mkdir(parents=True)
    command("git", "init", "-b", "main", cwd=path)
    command("git", "config", "user.email", "pulse@example.com", cwd=path)
    command("git", "config", "user.name", "Project Pulse", cwd=path)
    (path / "README.md").write_text("# Example\n")
    if package:
        (path / "package.json").write_text(json.dumps({"scripts": {"test": "node --test"}}))
    command("git", "add", ".", cwd=path)
    command("git", "commit", "-m", "Initial commit", cwd=path)


class ProjectPulseTests(unittest.TestCase):
    def test_pipelines_is_a_registered_one_shot_command(self):
        args = pulse.build_parser().parse_args(["pipelines"])
        self.assertEqual(args.command, "pipelines")
        self.assertFalse(pulse.wants_live_pane(args))

    def test_discovers_repositories_but_skips_archive(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            make_repo(root / "Active")
            make_repo(root / "_archive" / "Old")
            discovered = pulse.discover_repositories(root)
            self.assertEqual(discovered, [root / "Active"])

    def test_root_repo_does_not_hide_nested_repositories(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "workspace"
            make_repo(root)
            make_repo(root / "Product")
            discovered = pulse.discover_repositories(root)
            self.assertEqual(discovered, [root, root / "Product"])

    def test_dirty_repo_becomes_a_hygiene_recommendation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo_path = root / "Example"
            make_repo(repo_path)
            (repo_path / "README.md").write_text("changed\n")
            repo = pulse.inspect_repo(root, repo_path, datetime.now(timezone.utc))
            recommendations = pulse.repo_recommendations(repo)
            self.assertEqual(repo.unstaged, 1)
            self.assertTrue(any(item.category == "hygiene" for item in recommendations))

    def test_testable_repo_without_workflow_suggests_automation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo_path = root / "Example"
            make_repo(repo_path, package=True)
            repo = pulse.inspect_repo(root, repo_path, datetime.now(timezone.utc))
            recommendations = pulse.repo_recommendations(repo)
            self.assertTrue(repo.has_tests)
            self.assertFalse(repo.has_workflow)
            self.assertTrue(any(item.category == "workflow" for item in recommendations))

    def test_remoteless_repo_asks_to_be_published_until_it_declares_local_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo_path = root / "Example"
            make_repo(repo_path)
            repo = pulse.inspect_repo(root, repo_path, datetime.now(timezone.utc))
            self.assertFalse(repo.local_only)
            self.assertTrue(
                any("belongs on GitHub" in item.title for item in pulse.repo_recommendations(repo))
            )

            command("git", "config", "--bool", "pulse.localOnly", "true", cwd=repo_path)
            repo = pulse.inspect_repo(root, repo_path, datetime.now(timezone.utc))
            self.assertTrue(repo.local_only)
            self.assertFalse(
                any("belongs on GitHub" in item.title for item in pulse.repo_recommendations(repo))
            )

    def test_workflow_card_is_suppressed_until_a_starter_exists(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo_path = root / "Example"
            make_repo(repo_path, package=True)
            repo = pulse.inspect_repo(root, repo_path, datetime.now(timezone.utc))
            recommendations = pulse.repo_recommendations(repo, starter_exists=False)
            self.assertFalse(any(item.category == "workflow" for item in recommendations))

    def test_workflow_card_ignores_a_dirty_working_tree(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo_path = root / "Example"
            make_repo(repo_path, package=True)
            (repo_path / "README.md").write_text("changed\n")
            repo = pulse.inspect_repo(root, repo_path, datetime.now(timezone.utc))
            recommendations = pulse.repo_recommendations(repo)
            self.assertFalse(repo.clean)
            self.assertTrue(any(item.category == "workflow" for item in recommendations))

    def test_default_selection_diversifies_categories(self):
        items = [
            pulse.Recommendation(100, "hygiene", "A", "a", repo="one"),
            pulse.Recommendation(99, "hygiene", "B", "b", repo="two"),
            pulse.Recommendation(80, "terminal", "C", "c"),
            pulse.Recommendation(70, "workflow", "D", "d"),
        ]
        selected = pulse.select_recommendations(items, "anything", 3)
        self.assertEqual([item.category for item in selected], ["hygiene", "terminal", "workflow"])

    def test_dismissal_persists_and_hides_matching_item(self):
        with tempfile.TemporaryDirectory() as temporary:
            state_path = Path(temporary) / "state.json"
            item = pulse.Recommendation(80, "build", "Build a thing", "It would be useful")
            dismissals = pulse.dismiss_item(state_path, [], item)
            self.assertEqual(pulse.load_dismissals(state_path), dismissals)
            self.assertEqual(pulse.visible_recommendations([item], dismissals), [])

    def test_inspects_pipeline_logs_and_state_read_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pipeline = root / "AgentWorkflows" / "example-loop"
            (pipeline / "logs").mkdir(parents=True)
            (pipeline / "state" / "queue").mkdir(parents=True)
            (pipeline / "state" / "reviews" / "batch").mkdir(parents=True)
            (pipeline / "logs" / "run-20260101-000000-1.log").write_text("started\nAll tasks done.\n")
            (pipeline / "state" / "queue" / "task.json").write_text("{}")
            (pipeline / "state" / "reviews" / "batch" / "task1.json").write_text("{}")
            (pipeline / "state" / "reviews" / ".gitkeep").write_text("")
            (root / "AgentWorkflows" / "__pycache__").mkdir()

            pipelines = pulse.inspect_pipelines(root)
            self.assertEqual([item.name for item in pipelines], ["example-loop"])
            snapshot = pipelines[0]
            self.assertEqual(snapshot.run_count, 1)
            self.assertEqual(snapshot.last_line, "All tasks done.")
            self.assertEqual(snapshot.queued, 1)
            self.assertEqual(snapshot.review_files, 1)
            self.assertIsNotNone(snapshot.last_run_at)

    def test_pipeline_without_runs_reads_as_never_run(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "AgentWorkflows" / "quiet-loop" / "logs").mkdir(parents=True)
            pipelines = pulse.inspect_pipelines(root)
            self.assertEqual(len(pipelines), 1)
            self.assertIsNone(pipelines[0].last_run_at)
            output = io.StringIO()
            with redirect_stdout(output):
                pulse.print_pipelines(pipelines, pulse.Palette(False))
            self.assertIn("never run", output.getvalue())

    def test_configured_pipeline_footer_shows_ready_action_before_its_first_run(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pipeline = root / "AgentWorkflows" / "uniwork"
            (pipeline / "logs").mkdir(parents=True)
            (pipeline / "uniwork").write_text("#!/bin/sh\n")
            snapshot = pulse.inspect_pipelines(
                root, {"workflow_args": {"uniwork": 'APP "issue"'}}
            )[0]
            self.assertEqual(
                snapshot.start_command,
                'AgentWorkflows/uniwork/uniwork APP "issue"',
            )
            summary = pulse.compact_pipeline(snapshot)
            self.assertIn("UNIWORK / READY", summary)
            self.assertIn('START / AgentWorkflows/uniwork/uniwork APP "issue"', summary)
            self.assertNotIn("NEVER RUN", summary)

    def test_pipeline_footer_distinguishes_idle_and_queued_activity(self):
        last_run = datetime(2026, 8, 30, 19, 0, tzinfo=timezone.utc).isoformat()
        idle = pulse.Pipeline(name="uniwork", last_run_at=last_run, run_count=3)
        queued = pulse.Pipeline(
            name="uniwork", last_run_at=last_run, run_count=3, queued=2
        )
        self.assertIn("UNIWORK / IDLE", pulse.compact_pipeline(idle))
        self.assertIn("3 RUNS", pulse.compact_pipeline(idle))
        self.assertIn("UNIWORK / QUEUED 2", pulse.compact_pipeline(queued))

    def test_configured_pipelines_are_discovered_outside_the_workflows_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            capture_runner = root / "demo-app" / "scripts" / "record-capture.sh"
            capture_runner.parent.mkdir(parents=True)
            capture_runner.write_text("#!/bin/sh\n")
            monitor = root / "Scripts" / "agent-monitor"
            monitor.parent.mkdir()
            monitor.write_text("#!/usr/bin/env python3\n")
            capture_log = root / "logs" / "runs.log"
            capture_log.parent.mkdir()
            capture_log.write_text(
                "2026-08-30T18:52:50  first ok\n"
                "2026-08-30T19:43:02  second ok\n"
            )
            config = {
                "pipeline": [
                    {
                        "name": "demo-capture",
                        "detect": "demo-app/scripts/record-capture.sh",
                        "start_command": "cd demo-app && make capture",
                        "log": "logs/runs.log",
                    },
                    {
                        "name": "agent-monitor",
                        "detect": "Scripts/agent-monitor",
                        "start_command": "Scripts/agent-monitor [repo]",
                    },
                    {
                        "name": "not-installed",
                        "detect": "nowhere/at/all",
                    },
                ]
            }
            pipelines = pulse.inspect_pipelines(root, config)
        self.assertEqual(
            [pipeline.name for pipeline in pipelines],
            ["demo-capture", "agent-monitor"],
        )
        capture, monitor_snapshot = pipelines
        self.assertEqual(capture.run_count, 2)
        self.assertIn("second ok", capture.last_line)
        self.assertEqual(capture.start_command, "cd demo-app && make capture")
        self.assertEqual(monitor_snapshot.start_command, "Scripts/agent-monitor [repo]")

    def test_malformed_config_falls_back_to_defaults(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            broken = root / "pulse.toml"
            broken.write_text("this is not = valid = toml\n")
            self.assertEqual(pulse.load_config(broken), {})
            self.assertEqual(pulse.load_config(root / "absent.toml"), {})
            self.assertEqual(pulse.inspect_pipelines(root, {}), [])

    def test_root_resolution_prefers_flag_then_environment_then_config(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            flagged = root / "flagged"
            environment = root / "environment"
            configured = root / "configured"
            for path in (flagged, environment, configured):
                path.mkdir()
            config = {"root": str(configured)}
            with mock.patch.dict(os.environ, {"PROJECTS_ROOT": str(environment)}):
                self.assertEqual(pulse.resolve_root(flagged, config), flagged.resolve())
                self.assertEqual(pulse.resolve_root(None, config), environment.resolve())
            with mock.patch.dict(os.environ, {}, clear=True):
                self.assertEqual(pulse.resolve_root(None, config), configured.resolve())
                with mock.patch.object(pulse.Path, "home", return_value=root):
                    self.assertEqual(
                        pulse.resolve_root(None, {}), (root / "Projects").resolve()
                    )

    def test_three_workflows_all_fit_in_the_compact_footer(self):
        pipelines = [
            pulse.Pipeline(name="uniwork", start_command="uniwork"),
            pulse.Pipeline(
                name="demo-capture",
                last_run_at=datetime.now(timezone.utc).isoformat(),
                run_count=4,
            ),
            pulse.Pipeline(name="agent-monitor", start_command="agent-monitor"),
        ]
        output = io.StringIO()
        with redirect_stdout(output):
            pulse.print_instrument_footer(pipelines, pulse.Palette(False), 84, live=True)
        rendered = output.getvalue()
        self.assertIn("UNIWORK READY", rendered)
        self.assertIn("DEMO-CAPTURE IDLE · 4 RUNS", rendered)
        self.assertIn("AGENT-MONITOR READY", rendered)
        self.assertNotIn("MORE", rendered)
        self.assertLessEqual(max(len(line) for line in rendered.splitlines()), 84)

    def test_dashboard_leads_with_moves_and_ends_with_pipelines(self):
        repo = pulse.Repo(name="Example", path="/workspace/Example")
        pipeline = pulse.Pipeline(name="example-loop")
        item = pulse.Recommendation(80, "hygiene", "Close the loop", "Something is dirty.")
        github = pulse.GithubSnapshot(available=False, error="Skipped with --local")
        output = io.StringIO()
        with redirect_stdout(output):
            pulse.print_dashboard(Path("/workspace"), [repo], [pipeline], github, [item], pulse.Palette(False))
        rendered = output.getvalue()
        self.assertIn("GOOD NEXT MOVES", rendered)
        self.assertIn("AGENT PIPELINES", rendered)
        self.assertLess(rendered.index("GOOD NEXT MOVES"), rendered.index("AGENT PIPELINES"))

    def test_workspace_without_agentworkflows_has_no_pipelines(self):
        with tempfile.TemporaryDirectory() as temporary:
            self.assertEqual(pulse.inspect_pipelines(Path(temporary)), [])

    def test_agent_prompt_contains_context_and_stable_id(self):
        item = pulse.Recommendation(
            80,
            "github",
            "Publish the branch",
            "The branch is ahead.",
            "git push",
            "Example",
        )
        output = io.StringIO()
        with redirect_stdout(output):
            pulse.print_agent_prompt(item, Path("/workspace"))
        rendered = output.getvalue()
        self.assertIn("PROJECT PULSE TASK", rendered)
        self.assertIn("Publish the branch", rendered)
        self.assertIn(item.id, rendered)

    def test_listing_round_trips_and_survives_dismissals(self):
        with tempfile.TemporaryDirectory() as temporary:
            state_path = Path(temporary) / "state.json"
            items = [
                pulse.Recommendation(96, "github", "Publish A", "A is ahead.", "git push", "A"),
                pulse.Recommendation(94, "hygiene", "Tidy B", "B is dirty.", "git status", "B"),
            ]
            pulse.save_listing(state_path, "anything", list(enumerate(items, 1)))
            pulse.dismiss_item(state_path, pulse.load_dismissals(state_path), items[1])
            restored, shown_at = pulse.load_listing(state_path)
            self.assertEqual(restored, list(enumerate(items, 1)))
            self.assertIsNotNone(shown_at)
            self.assertEqual(len(pulse.load_dismissals(state_path)), 1)

    def test_numbers_survive_a_workspace_change_between_commands(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_path = root / "state.json"
            make_repo(root / "Alpha")
            make_repo(root / "Beta")
            (root / "Beta" / "scratch.txt").write_text("work in progress\n")
            listed = pulse.select_recommendations(
                pulse.all_recommendations(root, pulse.inspect_workspace(root), pulse.GithubSnapshot(False)),
                "anything",
                5,
            )
            pulse.save_listing(state_path, "anything", list(enumerate(listed, 1)))
            target = listed[-1]

            # The workspace moves on: Beta's open loop disappears and renumbers a fresh scan.
            (root / "Beta" / "scratch.txt").unlink()
            args = pulse.build_parser().parse_args(["prompt", str(len(listed)), "--local"])
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(pulse.act_on_item(args, root, state_path, []), 0)
            self.assertIn(target.id, output.getvalue())

    def test_acting_without_a_recent_list_is_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args = pulse.build_parser().parse_args(["prompt", "1", "--local"])
            self.assertEqual(pulse.act_on_item(args, root, root / "state.json", []), 2)

    def test_prompt_resolves_sparse_live_numbers(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_path = root / "state.json"
            make_repo(root / "Alpha")
            keeper = pulse.Recommendation(80, "build", "Keep me", "Still live", None, "Alpha")
            pulse.save_listing(state_path, "anything", [(1, keeper), (6, keeper)])
            args = pulse.build_parser().parse_args(["prompt", "6", "--local"])
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(pulse.act_on_item(args, root, state_path, []), 0)
            self.assertIn(keeper.id, output.getvalue())

            missing = pulse.build_parser().parse_args(["prompt", "3", "--local"])
            self.assertEqual(pulse.act_on_item(missing, root, state_path, []), 2)


def recommendation(name: str, category: str = "hygiene") -> "pulse.Recommendation":
    return pulse.Recommendation(80, category, f"Handle {name}", f"{name} needs attention.", None, name)


class VisualSystemTests(unittest.TestCase):
    def test_wordmark_is_three_rows_wide_and_slightly_arched(self):
        rows = pulse.wordmark_rows()
        self.assertEqual(len(rows), 3)
        self.assertEqual({len(row) for row in rows}, {58})
        self.assertTrue(rows[0].startswith("▄"))
        self.assertIn("█", rows[0])
        self.assertEqual({len(row) for row in pulse.wordmark_rows(3)}, {87})

    def test_wordmark_stays_compact_in_small_and_medium_panes(self):
        self.assertEqual(pulse.wordmark_scale(84), 1)
        self.assertEqual(pulse.wordmark_scale(107), 1)
        self.assertEqual(pulse.wordmark_scale(120), 2)

    def test_signal_gradient_uses_true_color_and_has_plain_fallback(self):
        colored = pulse.Palette(True).gradient("PULSE")
        self.assertIn("\033[38;2;255;76;176mP", colored)
        self.assertIn("\033[38;2;139;99;255mE", colored)
        self.assertEqual(pulse.Palette(False).gradient("PULSE"), "PULSE")

    def test_recommendation_categories_have_distinct_colors(self):
        palette = pulse.Palette(True)
        expected = {
            "hygiene": "250;84;185",
            "github": "145;90;240",
            "workflow": "72;124;240",
            "organize": "26;153;123",
            "build": "235;93;72",
            "terminal": "215;145;35",
        }
        rendered = {category: palette.category(category) for category in expected}
        for category, color in expected.items():
            self.assertIn(f"38;2;{color}", rendered[category])
        self.assertEqual(len(set(rendered.values())), len(expected))
        self.assertEqual(pulse.Palette(False).category("github"), "[  GITHUB  ]")

    def test_dashboard_has_masthead_instrument_plate_runway_and_footer(self):
        repo = pulse.Repo(name="Example", path="/workspace/Example", unstaged=1)
        pipeline = pulse.Pipeline(name="example-loop")
        item = recommendation("Example")
        github = pulse.GithubSnapshot(available=True, user="operator")
        output = io.StringIO()
        with mock.patch.object(pulse, "dashboard_width", return_value=96):
            with redirect_stdout(output):
                pulse.print_dashboard(
                    Path("/workspace"),
                    [repo],
                    [pipeline],
                    github,
                    [item],
                    pulse.Palette(False),
                )
        rendered = output.getvalue()
        self.assertIn("PROJECT PULSE // RECOMMENDATION INDEX", rendered)
        self.assertIn("01 REPOSITORIES", rendered)
        self.assertIn("01 OPEN LOOPS", rendered)
        self.assertIn("// GOOD NEXT MOVES", rendered)
        self.assertIn("AGENT PIPELINES", rendered)
        self.assertTrue(any("█" in line for line in rendered.splitlines()))
        self.assertLessEqual(max(len(line) for line in rendered.splitlines()), 96)

    def test_instrument_plate_keeps_the_terminal_background_transparent(self):
        output = io.StringIO()
        with redirect_stdout(output):
            pulse.print_instrument_band(
                Path("/workspace"),
                [],
                pulse.GithubSnapshot(False),
                pulse.Palette(True),
                80,
            )
        rendered = output.getvalue()
        self.assertIn("38;2;166;169;178", rendered)
        self.assertNotIn("48;2;", rendered)

    def test_five_item_live_pane_fits_reference_terminal(self):
        repos = [pulse.Repo(name="Example", path="/workspace/Example", unstaged=1)]
        slots = [
            pulse.Slot(
                index,
                pulse.Recommendation(
                    100 - index,
                    "hygiene",
                    f"Handle recommendation {index}",
                    "A deliberately long explanation that must remain on one clipped terminal line. " * 2,
                    "cd /workspace/Example && git status --short",
                    f"Example-{index}",
                ),
            )
            for index in range(1, 6)
        ]
        output = io.StringIO()
        with mock.patch.object(pulse, "dashboard_width", return_value=111):
            with mock.patch.object(pulse, "dashboard_height", return_value=36):
                with redirect_stdout(output):
                    pulse.print_live_dashboard(
                        Path("/workspace"),
                        repos,
                        [pulse.Pipeline(name="example-loop")],
                        pulse.GithubSnapshot(False),
                        slots,
                        pulse.Palette(False),
                        datetime.now(timezone.utc),
                        True,
                        slots[0].item.id,
                    )
        lines = output.getvalue().splitlines()
        self.assertLessEqual(len(lines), 36)
        self.assertLessEqual(max(len(line) for line in lines), 111)
        self.assertIn("[01]  [ HYGIENE  ]", output.getvalue())

    def test_compact_viewport_shows_three_moves_and_follows_selection(self):
        slots = [pulse.Slot(index, recommendation(str(index))) for index in range(1, 6)]
        shown = pulse.visible_live_slots(slots, slots[0].item.id, capacity=3)
        self.assertEqual([slot.number for slot in shown], [1, 2, 3])
        shown = pulse.visible_live_slots(slots, slots[3].item.id, capacity=3)
        self.assertEqual([slot.number for slot in shown], [2, 3, 4])
        shown = pulse.visible_live_slots(slots, slots[4].item.id, capacity=3)
        self.assertEqual([slot.number for slot in shown], [3, 4, 5])

    def test_compact_viewport_capacity_reserves_space_for_the_footer(self):
        github = pulse.GithubSnapshot(False)
        self.assertEqual(
            pulse.live_slot_capacity(27, 84, [], github, None, True, False),
            3,
        )
        self.assertGreaterEqual(
            pulse.live_slot_capacity(35, 113, [], github, None, True, False),
            5,
        )


class LiveSessionTests(unittest.TestCase):
    def test_numbers_are_never_reused_within_a_session(self):
        now = datetime.now(timezone.utc)
        session = pulse.LiveSession("anything", 2)
        first, second, third = recommendation("Alpha"), recommendation("Beta"), recommendation("Gamma")
        slots = session.refresh([first, second], now)
        self.assertEqual([slot.number for slot in slots], [1, 2])

        # Alpha resolves; Gamma arrives. Gamma must take 3, never Alpha's 1.
        slots = session.refresh([second, third], now)
        by_title = {slot.item.title: slot for slot in slots}
        self.assertEqual(by_title["Handle Gamma"].number, 3)
        self.assertTrue(by_title["Handle Alpha"].resolved)

    def test_resolved_items_linger_with_a_check_then_leave(self):
        start = datetime.now(timezone.utc)
        session = pulse.LiveSession("anything", 1)
        item = recommendation("Alpha")
        session.refresh([item], start)
        lingering = session.refresh([], start + timedelta(seconds=60))
        self.assertEqual(len(lingering), 1)
        self.assertTrue(lingering[0].resolved)
        gone = session.refresh([], start + timedelta(seconds=60 + pulse.RESOLVED_LINGER_SECONDS + 1))
        self.assertEqual(gone, [])

    def test_reappearing_item_reclaims_its_number(self):
        start = datetime.now(timezone.utc)
        session = pulse.LiveSession("anything", 1)
        first, second = recommendation("Alpha"), recommendation("Beta")
        session.refresh([first], start)
        session.refresh([second], start + timedelta(seconds=60))  # Alpha resolved, Beta takes 2
        slots = session.refresh([first, second], start + timedelta(seconds=120))
        by_title = {slot.item.title: slot for slot in slots}
        self.assertEqual(by_title["Handle Alpha"].number, 1)
        self.assertFalse(by_title["Handle Alpha"].resolved)
        self.assertEqual(by_title["Handle Beta"].number, 2)

    def test_dismissed_item_leaves_without_a_check(self):
        now = datetime.now(timezone.utc)
        session = pulse.LiveSession("anything", 2)
        first, second = recommendation("Alpha"), recommendation("Beta")
        session.refresh([first, second], now)
        slots = session.refresh([second], now, hidden=frozenset({first.id}))
        self.assertEqual([slot.item.title for slot in slots], ["Handle Beta"])

    def test_live_pane_render_marks_resolved_lines(self):
        slot_open = pulse.Slot(1, recommendation("Alpha"))
        slot_done = pulse.Slot(2, recommendation("Beta"), resolved_at=datetime.now(timezone.utc))
        output = io.StringIO()
        with redirect_stdout(output):
            pulse.print_live_recommendations([slot_open, slot_done], pulse.Palette(False))
        rendered = output.getvalue()
        self.assertIn("Handle Alpha", rendered)
        self.assertIn("✓ Handle Beta", rendered)

    def test_live_selection_wraps_and_skips_resolved_items(self):
        first = pulse.Slot(1, recommendation("Alpha"))
        resolved = pulse.Slot(2, recommendation("Beta"), resolved_at=datetime.now(timezone.utc))
        last = pulse.Slot(3, recommendation("Gamma"))
        slots = [first, resolved, last]

        self.assertEqual(pulse.move_live_selection(slots, None, 0), first.item.id)
        self.assertEqual(pulse.move_live_selection(slots, first.item.id, -1), last.item.id)
        self.assertEqual(pulse.move_live_selection(slots, last.item.id, 1), first.item.id)

    def test_only_selected_item_gets_a_compact_number_badge(self):
        first = pulse.Slot(1, recommendation("Alpha"))
        second = pulse.Slot(2, recommendation("Beta"))
        output = io.StringIO()
        with redirect_stdout(output):
            pulse.print_live_recommendations(
                [first, second], pulse.Palette(False), second.item.id, width=80
            )
        rendered = output.getvalue()
        self.assertIn("[02]  [ HYGIENE  ]  Handle Beta", rendered)
        self.assertNotIn("[01]", rendered)
        self.assertNotIn("╭", rendered)
        self.assertLessEqual(max(len(line) for line in rendered.splitlines()), 80)

    def test_copy_feedback_flashes_only_the_number_badge(self):
        slot = pulse.Slot(1, recommendation("Alpha"))
        visible = io.StringIO()
        hidden = io.StringIO()
        with redirect_stdout(visible):
            pulse.print_live_recommendations(
                [slot], pulse.Palette(True), slot.item.id, selection_visible=True, width=80
            )
        with redirect_stdout(hidden):
            pulse.print_live_recommendations(
                [slot], pulse.Palette(True), slot.item.id, selection_visible=False, width=80
            )
        self.assertEqual(visible.getvalue().count("\n"), hidden.getvalue().count("\n"))
        self.assertIn("48;2;250;84;185", visible.getvalue())
        self.assertIn("48;2;211;210;216", hidden.getvalue())
        self.assertEqual(visible.getvalue().count("48;2;"), 1)

    def test_copy_uses_the_exact_agent_prompt(self):
        item = recommendation("Alpha")
        expected = io.StringIO()
        with redirect_stdout(expected):
            pulse.print_agent_prompt(item, Path("/workspace"))
        with mock.patch.object(pulse.subprocess, "run") as run:
            self.assertTrue(pulse.copy_agent_prompt(item, Path("/workspace")))
        run.assert_called_once_with(["pbcopy"], input=expected.getvalue(), text=True, check=True)

    def test_live_key_reader_recognizes_arrows_and_enter(self):
        reads = iter((b"\x1b", b"[", b"A"))
        with mock.patch.object(pulse.select, "select", side_effect=[([4], [], []), ([4], [], []), ([4], [], []), ([], [], [])]):
            with mock.patch.object(pulse.os, "read", side_effect=lambda _fd, _size: next(reads)):
                self.assertEqual(pulse.read_live_key(4, 1), "up")

        with mock.patch.object(pulse.select, "select", return_value=([4], [], [])):
            with mock.patch.object(pulse.os, "read", return_value=b"\r"):
                self.assertEqual(pulse.read_live_key(4, 1), "enter")

    def test_ctrl_r_reads_as_a_refresh_key(self):
        with mock.patch.object(pulse.select, "select", return_value=([4], [], [])):
            with mock.patch.object(pulse.os, "read", return_value=b"\x12"):
                self.assertEqual(pulse.read_live_key(4, 1), "refresh")

    def test_refresh_key_rescans_immediately_including_github(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_path = root / "state.json"
            item = recommendation("Alpha")
            args = pulse.build_parser().parse_args([])
            output = io.StringIO()
            with mock.patch.object(pulse, "live_input", return_value=nullcontext(4)):
                with mock.patch.object(pulse, "read_live_key", side_effect=["refresh", KeyboardInterrupt]):
                    with mock.patch.object(pulse, "inspect_workspace", return_value=[]) as scan:
                        with mock.patch.object(pulse, "inspect_pipelines", return_value=[]):
                            with mock.patch.object(
                                pulse, "fetch_github", return_value=pulse.GithubSnapshot(False)
                            ) as fetch:
                                with mock.patch.object(pulse, "all_recommendations", return_value=[item]):
                                    with redirect_stdout(output):
                                        self.assertEqual(
                                            pulse.run_live(args, root, state_path, pulse.Palette(False)), 0
                                        )
            self.assertEqual(scan.call_count, 2)
            self.assertEqual(fetch.call_count, 2)

    def test_enter_copies_selected_prompt_and_keeps_live_pane_running(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_path = root / "state.json"
            item = recommendation("Alpha")
            args = pulse.build_parser().parse_args(["--local"])
            output = io.StringIO()
            with mock.patch.object(pulse, "live_input", return_value=nullcontext(4)):
                with mock.patch.object(pulse, "read_live_key", side_effect=["enter", KeyboardInterrupt]):
                    with mock.patch.object(pulse, "inspect_workspace", return_value=[]):
                        with mock.patch.object(pulse, "inspect_pipelines", return_value=[]):
                            with mock.patch.object(pulse, "fetch_github", return_value=pulse.GithubSnapshot(False)):
                                with mock.patch.object(pulse, "all_recommendations", return_value=[item]):
                                    with mock.patch.object(pulse, "copy_agent_prompt", return_value=True) as copy:
                                        with mock.patch.object(pulse.time, "sleep"):
                                            with redirect_stdout(output):
                                                self.assertEqual(
                                                    pulse.run_live(
                                                        args, root, state_path, pulse.Palette(False)
                                                    ),
                                                    0,
                                                )
            copy.assert_called_once_with(item, root)
            self.assertGreaterEqual(output.getvalue().count("Handle Alpha"), 3)

    def test_resizing_repaints_without_waiting_for_the_next_scan(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_path = root / "state.json"
            args = pulse.build_parser().parse_args(["--local"])
            size = os.terminal_size
            with mock.patch.object(pulse, "live_input", return_value=nullcontext(4)):
                with mock.patch.object(
                    pulse, "read_live_key", side_effect=[None, KeyboardInterrupt]
                ):
                    with mock.patch.object(pulse, "inspect_workspace", return_value=[]):
                        with mock.patch.object(pulse, "inspect_pipelines", return_value=[]):
                            with mock.patch.object(
                                pulse, "fetch_github", return_value=pulse.GithubSnapshot(False)
                            ):
                                with mock.patch.object(pulse, "all_recommendations", return_value=[]):
                                    with mock.patch.object(
                                        pulse.shutil,
                                        "get_terminal_size",
                                        side_effect=[size((113, 35)), size((84, 27))],
                                    ):
                                        with mock.patch.object(
                                            pulse, "print_live_dashboard", return_value=(None, 0)
                                        ) as dashboard:
                                            with redirect_stdout(io.StringIO()):
                                                self.assertEqual(
                                                    pulse.run_live(
                                                        args,
                                                        root,
                                                        state_path,
                                                        pulse.Palette(False),
                                                    ),
                                                    0,
                                                )
            self.assertEqual(dashboard.call_count, 2)


class LivePaneOwnershipTests(unittest.TestCase):
    def test_a_fresh_live_marker_from_an_alive_process_owns_numbering(self):
        with tempfile.TemporaryDirectory() as temporary:
            state_path = Path(temporary) / "state.json"
            # PID 1 is alive on every Unix and is never this test process.
            pulse.write_state(state_path, {"live": {
                "pid": 1, "updated_at": datetime.now(timezone.utc).isoformat(),
            }})
            self.assertTrue(pulse.live_pane_owns_numbering(state_path))

    def test_a_stale_marker_or_our_own_pid_does_not_own_numbering(self):
        with tempfile.TemporaryDirectory() as temporary:
            state_path = Path(temporary) / "state.json"
            stale = datetime.now(timezone.utc) - timedelta(seconds=pulse.LIVE_MARKER_TTL_SECONDS + 1)
            pulse.write_state(state_path, {"live": {"pid": 1, "updated_at": stale.isoformat()}})
            self.assertFalse(pulse.live_pane_owns_numbering(state_path))

            pulse.write_state(state_path, {"live": {
                "pid": os.getpid(), "updated_at": datetime.now(timezone.utc).isoformat(),
            }})
            self.assertFalse(pulse.live_pane_owns_numbering(state_path))

    def test_interrupting_the_live_loop_clears_the_marker_but_keeps_the_listing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_path = root / "state.json"
            args = pulse.build_parser().parse_args(["--local"])
            output = io.StringIO()
            with mock.patch.object(pulse.time, "sleep", side_effect=KeyboardInterrupt):
                with redirect_stdout(output):
                    self.assertEqual(pulse.run_live(args, root, state_path, pulse.Palette(False)), 0)
            state = pulse.read_state(state_path)
            self.assertNotIn("live", state)
            self.assertIn("listing", state)
            rendered = output.getvalue()
            self.assertTrue(rendered.startswith(pulse.ALT_SCREEN_ENTER + pulse.CURSOR_HIDE))
            self.assertTrue(rendered.endswith(pulse.CURSOR_SHOW + pulse.ALT_SCREEN_LEAVE))

    def test_clearing_the_marker_releases_ownership_but_keeps_the_listing(self):
        with tempfile.TemporaryDirectory() as temporary:
            state_path = Path(temporary) / "state.json"
            pulse.save_listing(state_path, "anything", [(4, recommendation("Alpha"))], live_pid=1)
            self.assertTrue(pulse.live_pane_owns_numbering(state_path))
            pulse.clear_live_marker(state_path)
            self.assertFalse(pulse.live_pane_owns_numbering(state_path))
            listing, _shown_at = pulse.load_listing(state_path)
            self.assertEqual([number for number, _item in listing], [4])



def idea(kind: str = "automation", title: str = "Ship a digest") -> dict:
    return {
        "kind": kind,
        "title": title,
        "detail": f"{title} is not something a git scan can suggest.",
        "prompt": f"Build {title.lower()} and verify it end to end.",
    }


class IdeaContractTests(unittest.TestCase):
    def test_ideas_are_rejected_whole_rather_than_stored_half_valid(self):
        for payload, reason in (
            ({"ideas": []}, "empty"),
            ({"ideas": [{**idea(), "kind": "refactor"}]}, "unknown kind"),
            ({"ideas": [{**idea(), "prompt": "  "}]}, "blank prompt"),
            ({"ideas": [{**idea(), "detail": ""}]}, "blank detail"),
            ({"ideas": ["not an object"]}, "not an object"),
            ({"ideas": [idea(title=str(n)) for n in range(10)] + [{**idea(), "prompt": ""}]}, "bad 11th"),
        ):
            with self.subTest(reason=reason):
                with self.assertRaises(pulse.IdeaError):
                    pulse.parse_ideas(payload)

    def test_a_fenced_or_chatty_reply_still_yields_ideas(self):
        body = json.dumps({"ideas": [idea()]})
        for wrapper in (body, f"```json\n{body}\n```", f"Here you go:\n{body}\nHope that helps."):
            with self.subTest(wrapper=wrapper[:12]):
                self.assertEqual(len(pulse.parse_ideas(pulse.extract_json(wrapper))), 1)

    def test_prose_without_json_is_an_error_not_an_empty_band(self):
        with self.assertRaises(pulse.IdeaError):
            pulse.extract_json("I could not think of anything today.")

    def test_kinds_are_lowercased_before_validation(self):
        parsed = pulse.parse_ideas({"ideas": [{**idea(), "kind": "Automation"}]})
        self.assertEqual(parsed[0]["kind"], "automation")

    def test_merge_keeps_untouched_ideas_and_only_fills_free_slots(self):
        existing = [idea("automation", "Alpha"), idea("agent", "Beta")]
        incoming = [idea("script", "Gamma"), idea("tech", "Delta"), idea("script", "Epsilon")]
        merged = pulse.merge_ideas(existing, incoming, free=2)
        self.assertEqual([entry["title"] for entry in merged], ["Alpha", "Beta", "Gamma", "Delta"])

    def test_merge_does_not_restate_an_idea_already_in_the_band(self):
        existing = [idea("automation", "Alpha")]
        merged = pulse.merge_ideas(existing, [idea("automation", "alpha"), idea("agent", "Beta")], free=4)
        self.assertEqual([entry["title"] for entry in merged], ["Alpha", "Beta"])

    def test_spread_holds_back_a_crowded_kind_rather_than_repeat_itself(self):
        crowded = [idea("script", name) for name in ("A", "B", "C")] + [idea("agent", "D")]
        kept = pulse.enforce_spread([], crowded)
        self.assertEqual([entry["title"] for entry in kept], ["A", "B", "D"])

    def test_spread_admits_a_held_back_idea_once_every_kind_is_present(self):
        full = [idea("script", name) for name in ("A", "B", "C")] + [idea(kind, kind) for kind in ("agent", "automation", "tech")]
        kept = pulse.enforce_spread([], full)
        self.assertEqual(len(kept), 6)
        self.assertEqual(kept[-1]["title"], "C")  # admitted last, so it is the first to miss the cut

    def test_spread_is_enforced_on_what_comes_in_never_on_what_is_kept(self):
        # Two kept scripts already; a pass that brings two more scripts and one
        # agent may only add the agent. The kept scripts are the user's.
        kept = [idea("script", "Kept A"), idea("script", "Kept B")]
        incoming = [idea("script", "New C"), idea("script", "New D"), idea("agent", "New E")]
        merged = pulse.merge_ideas(kept, incoming, free=4)
        self.assertEqual([entry["title"] for entry in merged], ["Kept A", "Kept B", "New E"])
        # Spread can hold a pass short; the cut never reaches into kept ideas.
        self.assertEqual(pulse.merge_ideas(kept, incoming, free=0), kept)


class IdeaGeneratorTests(unittest.TestCase):
    def test_a_failed_refresh_leaves_the_stored_band_untouched(self):
        with tempfile.TemporaryDirectory() as temporary:
            state_path = Path(temporary) / "state.json"
            pulse.save_ideas(state_path, {"items": [idea("agent", "Keeper")], "done": []})
            failures = (
                subprocess.CompletedProcess([], 1, "", "claude: not logged in"),
                subprocess.CompletedProcess([], 0, "not json at all", ""),
                subprocess.CompletedProcess([], 0, json.dumps({"result": "sorry, no"}), ""),
                subprocess.CompletedProcess([], 0, json.dumps({"is_error": True, "result": "rate limited"}), ""),
            )
            for completed in failures:
                with self.subTest(returncode=completed.returncode):
                    with mock.patch.object(pulse.shutil, "which", return_value="/bin/claude"):
                        with mock.patch.object(pulse, "run", return_value=completed):
                            with self.assertRaises(pulse.IdeaError):
                                pulse.refresh_ideas(state_path, Path(temporary), [], [], pulse.IDEA_MODEL)
                    stored = pulse.load_ideas(state_path)
                    self.assertEqual([entry["title"] for entry in stored["items"]], ["Keeper"])

    def test_a_missing_claude_cli_is_reported_not_crashed_through(self):
        with mock.patch.object(pulse.shutil, "which", return_value=None):
            with self.assertRaises(pulse.IdeaError):
                pulse.generate_ideas({"need": 3})

    def test_the_generator_is_asked_for_fable_and_fed_the_pack_on_stdin(self):
        envelope = json.dumps({"result": json.dumps({"ideas": [idea()]})})
        with mock.patch.object(pulse.shutil, "which", return_value="/bin/claude"):
            with mock.patch.object(
                pulse, "run", return_value=subprocess.CompletedProcess([], 0, envelope, "")
            ) as runner:
                parsed = pulse.generate_ideas({"need": 3, "marker": "pack-goes-here"})
        command, = runner.call_args.args
        self.assertIn("--model", command)
        self.assertEqual(command[command.index("--model") + 1], "claude-fable-5")
        self.assertIn("pack-goes-here", runner.call_args.kwargs["stdin"])
        self.assertEqual(len(parsed), 1)

    def test_the_pack_tells_the_model_what_it_already_said_and_what_was_done(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "LEDGER.md").write_text("# Ledger\n- a commit\n")
            (root / "Scripts").mkdir()
            (root / "Scripts" / "cmds").write_text("#!/bin/sh\n")
            stored = {
                "items": [idea("agent", "Already said")],
                "done": [{"id": "abc", "title": "Already built", "kind": "script"}],
            }
            pack = pulse.build_idea_pack(root, [], stored, [], need=3)
            self.assertEqual(pack["existing_ideas"], [{"kind": "agent", "title": "Already said"}])
            self.assertEqual(pack["recent_done"], [{"kind": "script", "title": "Already built"}])
            self.assertIn("Scripts/cmds", pack["tooling"])
            self.assertIn("a commit", pack["ledger"])
            self.assertEqual(pack["need"], 3)

    def test_the_pack_sends_recent_ledger_entries_not_the_preamble(self):
        entries = "".join(f"- `repo` · {n:07x} — Entry {n}\n  > why it changed\n" for n in range(1, 41))
        text = f"# Ledger\n\nGenerated — do not hand-edit.\n\n## 2026-08-30\n\n{entries}"
        ledger = pulse.recent_ledger_entries(text)
        self.assertNotIn("do not hand-edit", ledger)
        self.assertTrue(ledger.startswith("## 2026-08-30"))
        self.assertIn("Entry 30\n", ledger)
        self.assertNotIn("Entry 31", ledger)
        self.assertIsNone(pulse.recent_ledger_entries(""))

    def test_the_pack_tells_the_model_what_was_dismissed(self):
        with tempfile.TemporaryDirectory() as temporary:
            stored = {"items": [idea("agent", "Unwanted"), idea("tech", "Wanted")], "done": []}
            hidden = pulse.idea_recommendation(stored["items"][0])
            dismissals = [{"id": hidden.id, "title": hidden.title, "category": "agent"}]
            pack = pulse.build_idea_pack(Path(temporary), [], stored, dismissals, need=3)
            self.assertEqual(pack["existing_ideas"], [{"kind": "tech", "title": "Wanted"}])
            self.assertEqual(pack["dismissed"], [{"kind": "agent", "title": "Unwanted"}])


class IdeaBandTests(unittest.TestCase):
    def test_ideas_are_numbered_after_the_runway_not_ranked_against_it(self):
        github = pulse.GithubSnapshot(available=False, error="Skipped with --local")
        moves = [recommendation("Alpha"), recommendation("Beta")]
        ideas = [pulse.idea_recommendation(idea("tech", "Try something new"))]
        output = io.StringIO()
        with redirect_stdout(output):
            pulse.print_dashboard(
                Path("/workspace"), [], [], github, moves, pulse.Palette(False), ideas, None
            )
        rendered = output.getvalue()
        self.assertLess(rendered.index("GOOD NEXT MOVES"), rendered.index("WORTH CONSIDERING"))
        self.assertIn("03  [   TECH   ]  Try something new", rendered)

    def test_an_empty_band_says_how_to_fill_it(self):
        output = io.StringIO()
        with redirect_stdout(output):
            pulse.print_ideas_band([], pulse.Palette(False), 84, None)
        self.assertIn("pulse ideas --refresh", output.getvalue())

    def test_an_idea_advertises_its_prompt_instead_of_a_shell_command(self):
        item = pulse.idea_recommendation(idea())
        self.assertEqual(pulse.handoff_line(item), "build prompt ready / enter copies it")
        self.assertEqual(pulse.handoff_line(recommendation("Alpha")), "scope / Alpha")

    def test_the_idea_prompt_is_printed_as_written_not_rewrapped(self):
        item = pulse.idea_recommendation(idea())
        output = io.StringIO()
        with redirect_stdout(output):
            pulse.print_agent_prompt(item, Path("/workspace"))
        rendered = output.getvalue()
        # The generator's prompt is what gets pasted: nothing precedes it, and
        # only the Pulse ID trails it so a transcript can be traced back.
        self.assertTrue(rendered.startswith(item.prompt + "\n"))
        self.assertEqual(rendered.rstrip().splitlines()[-1], f"Pulse ID: {item.id}")
        self.assertNotIn("PROJECT PULSE", rendered)

    def test_the_band_never_pushes_the_runway_off_a_short_viewport(self):
        self.assertEqual(pulse.split_live_capacity(10, 5, 5), (5, 5))
        self.assertEqual(pulse.split_live_capacity(6, 5, 5), (5, 1))
        self.assertEqual(pulse.split_live_capacity(3, 5, 5), (2, 1))
        self.assertEqual(pulse.split_live_capacity(1, 5, 5), (1, 0))
        self.assertEqual(pulse.split_live_capacity(8, 5, 0), (8, 0))

    def test_a_short_viewport_collapses_the_band_instead_of_overflowing(self):
        github = pulse.GithubSnapshot(False)
        slots = [pulse.Slot(index, recommendation(str(index))) for index in range(1, 6)]
        ideas = [pulse.Slot(index, pulse.idea_recommendation(idea("tech", f"Idea {index}"))) for index in range(6, 11)]

        def render(idea_slots):
            output = io.StringIO()
            with mock.patch.object(pulse, "dashboard_width", return_value=111):
                with mock.patch.object(pulse, "dashboard_height", return_value=20):
                    with redirect_stdout(output):
                        pulse.print_live_dashboard(
                            Path("/workspace"), [], [], github, slots, pulse.Palette(False),
                            None, True, ideas[0].item.id, True, idea_slots, None,
                        )
            return output.getvalue()

        with mock.patch.object(pulse, "dashboard_width", return_value=111):
            with mock.patch.object(pulse, "dashboard_height", return_value=20):
                self.assertEqual(pulse.live_layout([], github, None, True, 5, 5)[1], 0)
        collapsed, bare = render(ideas), render(None)
        self.assertNotIn("NO IDEAS YET", collapsed)
        with mock.patch.object(pulse, "dashboard_width", return_value=111):
            with mock.patch.object(pulse, "dashboard_height", return_value=24):
                clipped = io.StringIO()
                with redirect_stdout(clipped):
                    pulse.print_live_dashboard(
                        Path("/workspace"), [], [], github, slots, pulse.Palette(False),
                        None, True, ideas[0].item.id, True, ideas, None,
                    )
        self.assertIn("05 IDEAS / REFRESH ON REQUEST", clipped.getvalue())  # the band, not the viewport
        self.assertIn("05 IDEAS / TERMINAL TOO SHORT", collapsed)
        self.assertLessEqual(len(collapsed.splitlines()), len(bare.splitlines()) + 2)

    def test_arrows_skip_a_band_the_terminal_cannot_show(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_path = root / "state.json"
            pulse.save_ideas(state_path, {"items": [idea("tech", "Hidden idea")], "done": []})
            move = recommendation("Alpha")
            args = pulse.build_parser().parse_args(["--local"])
            with mock.patch.object(pulse, "live_input", return_value=nullcontext(4)):
                with mock.patch.object(pulse, "read_live_key", side_effect=["down", "enter", KeyboardInterrupt]):
                    with mock.patch.object(pulse, "inspect_workspace", return_value=[]):
                        with mock.patch.object(pulse, "inspect_pipelines", return_value=[]):
                            with mock.patch.object(pulse, "fetch_github", return_value=pulse.GithubSnapshot(False)):
                                with mock.patch.object(pulse, "all_recommendations", return_value=[move]):
                                    with mock.patch.object(pulse, "dashboard_height", return_value=22):
                                        with mock.patch.object(pulse, "terminal_columns", return_value=111):
                                            with mock.patch.object(pulse, "copy_agent_prompt", return_value=True) as copy:
                                                with mock.patch.object(pulse.time, "sleep"):
                                                    with redirect_stdout(io.StringIO()):
                                                        pulse.run_live(args, root, state_path, pulse.Palette(False))
            # Down did not land on the idea the pane could not draw; the listing still numbers it.
            copy.assert_called_once_with(move, root)
            listing, _ = pulse.load_listing(state_path)
            self.assertEqual([number for number, _item in listing], [1, 2])

    def test_an_empty_band_says_how_to_fill_it_when_the_pane_has_room(self):
        github = pulse.GithubSnapshot(False)
        slots = [pulse.Slot(index, recommendation(str(index))) for index in range(1, 6)]
        for height, expected in ((53, True), (36, False)):
            with self.subTest(height=height):
                output = io.StringIO()
                with mock.patch.object(pulse, "dashboard_width", return_value=103):
                    with mock.patch.object(pulse, "dashboard_height", return_value=height):
                        with redirect_stdout(output):
                            pulse.print_live_dashboard(
                                Path("/workspace"), [], [], github, slots, pulse.Palette(False), None, True
                            )
                self.assertEqual("pulse ideas --refresh" in output.getvalue(), expected)
                self.assertLessEqual(len(output.getvalue().splitlines()), height)

    def test_an_open_band_costs_the_runway_rows(self):
        github = pulse.GithubSnapshot(False)
        without = pulse.live_slot_capacity(35, 113, [], github, None, True, False)
        with_band = pulse.live_slot_capacity(35, 113, [], github, None, True, False, idea_header_rows=2)
        self.assertLessEqual(with_band, without)

    def test_ideas_keep_their_numbers_and_leave_only_when_retired(self):
        session = pulse.LiveSession("anything", 5)
        first = pulse.idea_recommendation(idea("agent", "Alpha"))
        second = pulse.idea_recommendation(idea("script", "Beta"))
        session.refresh([], datetime.now(timezone.utc))
        self.assertEqual([slot.number for slot in session.refresh_ideas([first, second])], [1, 2])
        self.assertEqual([slot.number for slot in session.refresh_ideas([second])], [2])
        # An idea never shows a resolved tick: nothing in a scan can clear one.
        self.assertTrue(all(not slot.resolved for slot in session.refresh_ideas([second])))


class IdeaLifecycleTests(unittest.TestCase):
    def test_did_retires_an_idea_and_frees_its_slot(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_path = root / "state.json"
            entries = [idea("agent", "Alpha"), idea("script", "Beta")]
            pulse.save_ideas(state_path, {"items": entries, "done": []})
            stored = pulse.load_ideas(state_path)
            items = pulse.visible_ideas(stored, [])
            pulse.save_listing(state_path, "anything", list(enumerate(items, 1)))
            args = pulse.build_parser().parse_args(["did", "1", "--local"])
            with redirect_stdout(io.StringIO()):
                self.assertEqual(pulse.act_on_item(args, root, state_path, []), 0)
            remaining = pulse.visible_ideas(pulse.load_ideas(state_path), [])
            self.assertEqual([item.title for item in remaining], ["Beta"])
            # The freed slot is refilled, and the surviving idea is not rewritten.
            refilled = pulse.merge_ideas(
                [entry for entry in entries if entry["title"] == "Beta"], [idea("tech", "Gamma")], free=4
            )
            self.assertEqual([entry["title"] for entry in refilled], ["Beta", "Gamma"])

    def test_a_dismissed_idea_survives_a_refresh_so_restore_means_something(self):
        with tempfile.TemporaryDirectory() as temporary:
            state_path = Path(temporary) / "state.json"
            pulse.save_ideas(state_path, {"items": [idea("agent", "Alpha"), idea("script", "Beta")], "done": []})
            stored = pulse.load_ideas(state_path)
            alpha = pulse.visible_ideas(stored, [])[0]
            dismissals = pulse.dismiss_item(state_path, [], alpha)
            # The refresh proposes Alpha again under the same name; it must not sneak back in.
            stored = pulse.store_ideas(
                state_path, stored, dismissals, [idea("agent", "alpha"), idea("tech", "Gamma")], 5, "hand-written"
            )
            self.assertEqual([item.title for item in pulse.visible_ideas(stored, dismissals)], ["Beta", "Gamma"])
            self.assertEqual(sorted(entry["title"] for entry in stored["items"]), ["Alpha", "Beta", "Gamma"])
            self.assertEqual([item.title for item in pulse.visible_ideas(stored, [])], ["Beta", "Gamma", "Alpha"])

    def test_a_full_band_refuses_to_refresh_rather_than_spend_tokens_for_nothing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_path = root / "state.json"
            full = {"items": [idea(kind, kind) for kind in pulse.IDEA_KINDS] + [idea("tech", "Fifth")], "done": []}
            pulse.save_ideas(state_path, {**full, "generated_at": "2026-08-01T00:00:00+00:00"})
            source = root / "ideas.json"
            source.write_text(json.dumps({"ideas": [idea("agent", "Sixth")]}))
            for flags in (["--refresh"], ["--pack"], ["--set", str(source)]):
                with self.subTest(flags=flags):
                    args = pulse.build_parser().parse_args(["ideas", *flags, "--local"])
                    errors = io.StringIO()
                    with mock.patch.object(pulse, "run") as runner:
                        with mock.patch.object(pulse.sys, "stderr", errors):
                            with redirect_stdout(io.StringIO()):
                                self.assertEqual(pulse.act_on_ideas(args, root, state_path, [], pulse.Palette(False)), 1)
                    runner.assert_not_called()
                    self.assertIn("band is full (5/5)", errors.getvalue())
            self.assertEqual(pulse.load_ideas(state_path)["generated_at"], "2026-08-01T00:00:00+00:00")
            # Raising the limit is the way through.
            args = pulse.build_parser().parse_args(["ideas", "--set", str(source), "--ideas", "6", "--local"])
            with redirect_stdout(io.StringIO()):
                self.assertEqual(pulse.act_on_ideas(args, root, state_path, [], pulse.Palette(False)), 0)
            self.assertEqual(len(pulse.load_ideas(state_path)["items"]), 6)

    def test_showing_an_empty_band_keeps_the_dashboard_numbering(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_path = root / "state.json"
            pulse.save_listing(state_path, "anything", [(1, recommendation("Alpha")), (2, recommendation("Beta"))])
            args = pulse.build_parser().parse_args(["ideas", "--local"])
            with redirect_stdout(io.StringIO()):
                self.assertEqual(pulse.act_on_ideas(args, root, state_path, [], pulse.Palette(False)), 0)
            listing, _ = pulse.load_listing(state_path)
            self.assertEqual([item.title for _number, item in listing], ["Handle Alpha", "Handle Beta"])

    def test_a_set_file_that_is_not_utf8_is_refused_not_crashed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_path = root / "state.json"
            source = root / "ideas.json"
            source.write_bytes(b'{"ideas": [{"kind": "agent", "title": "caf\xe9"}]}')
            args = pulse.build_parser().parse_args(["ideas", "--set", str(source), "--local"])
            errors = io.StringIO()
            with mock.patch.object(pulse.sys, "stderr", errors):
                self.assertEqual(pulse.act_on_ideas(args, root, state_path, [], pulse.Palette(False)), 1)
            self.assertIn("existing ideas are unchanged", errors.getvalue())

    def test_did_refuses_a_state_based_move_and_points_at_dismiss(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_path = root / "state.json"
            pulse.save_listing(state_path, "anything", [(1, recommendation("Alpha"))])
            args = pulse.build_parser().parse_args(["did", "1", "--local"])
            errors = io.StringIO()
            with mock.patch.object(pulse.sys, "stderr", errors):
                self.assertEqual(pulse.act_on_item(args, root, state_path, []), 2)
            self.assertIn("pulse dismiss", errors.getvalue())

    def test_a_dismissed_idea_leaves_the_band_without_being_retired(self):
        with tempfile.TemporaryDirectory() as temporary:
            state_path = Path(temporary) / "state.json"
            entries = [idea("agent", "Alpha"), idea("script", "Beta")]
            pulse.save_ideas(state_path, {"items": entries, "done": []})
            stored = pulse.load_ideas(state_path)
            hidden = pulse.visible_ideas(stored, [])[0]
            dismissals = pulse.dismiss_item(state_path, [], hidden)
            self.assertEqual([item.title for item in pulse.visible_ideas(stored, dismissals)], ["Beta"])

    def test_a_hand_written_set_and_a_model_refresh_store_the_same_shape(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_path = root / "state.json"
            source = root / "ideas.json"
            source.write_text(json.dumps({"ideas": [idea("agent", "Alpha")]}))
            args = pulse.build_parser().parse_args(["ideas", "--set", str(source), "--local", "--no-color"])
            with redirect_stdout(io.StringIO()):
                self.assertEqual(pulse.act_on_ideas(args, root, state_path, [], pulse.Palette(False)), 0)
            byhand = pulse.load_ideas(state_path)
            self.assertEqual(byhand["model"], "hand-written")
            self.assertEqual([entry["title"] for entry in byhand["items"]], ["Alpha"])

            envelope = json.dumps({"result": json.dumps({"ideas": [idea("script", "Beta")]})})
            with mock.patch.object(pulse.shutil, "which", return_value="/bin/claude"):
                with mock.patch.object(
                    pulse, "run", return_value=subprocess.CompletedProcess([], 0, envelope, "")
                ):
                    refreshed = pulse.refresh_ideas(state_path, root, [], [], pulse.IDEA_MODEL)
            self.assertEqual([entry["title"] for entry in refreshed["items"]], ["Alpha", "Beta"])
            self.assertEqual(set(byhand["items"][0]), set(refreshed["items"][1]))

    def test_a_broken_set_file_is_refused_and_changes_nothing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_path = root / "state.json"
            pulse.save_ideas(state_path, {"items": [idea("agent", "Keeper")], "done": []})
            source = root / "ideas.json"
            source.write_text('{"ideas": [{"kind": "refactor", "title": "x", "detail": "y", "prompt": "z"}]}')
            args = pulse.build_parser().parse_args(["ideas", "--set", str(source), "--local"])
            with mock.patch.object(pulse.sys, "stderr", io.StringIO()):
                self.assertEqual(pulse.act_on_ideas(args, root, state_path, [], pulse.Palette(False)), 1)
            self.assertEqual([entry["title"] for entry in pulse.load_ideas(state_path)["items"]], ["Keeper"])

    def test_corrupt_stored_ideas_are_dropped_rather_than_rendered(self):
        with tempfile.TemporaryDirectory() as temporary:
            state_path = Path(temporary) / "state.json"
            pulse.write_state(
                state_path,
                {"ideas": {"items": [idea(), {"kind": "agent"}, "nonsense"], "done": ["bad"]}},
            )
            stored = pulse.load_ideas(state_path)
            self.assertEqual(len(stored["items"]), 1)
            self.assertEqual(stored["done"], [])

    def test_idea_flags_are_refused_outside_the_ideas_command(self):
        with mock.patch.object(pulse.sys, "stderr", io.StringIO()) as errors:
            self.assertEqual(pulse.main(["next", "--refresh", "--local"]), 2)
            self.assertIn("pulse ideas", errors.getvalue())

    def test_a_listing_written_before_the_band_existed_still_loads(self):
        with tempfile.TemporaryDirectory() as temporary:
            state_path = Path(temporary) / "state.json"
            legacy = {
                field: getattr(recommendation("Alpha"), field) for field in pulse.LISTING_FIELDS
            }
            pulse.write_state(
                state_path,
                {"listing": {"shown_at": "2026-08-30T00:00:00+00:00", "vibe": "anything",
                             "items": [{"number": 4, **legacy}]}},
            )
            listing, _shown_at = pulse.load_listing(state_path)
            self.assertEqual([number for number, _item in listing], [4])
            self.assertIsNone(listing[0][1].prompt)


def write_jsonl(path: Path, records: list, partial: str | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")
        if partial is not None:
            handle.write(partial)


def claude_home(home: Path, session_id: str, cwd: str, records: list, pid: int | None = None) -> Path:
    registry = home / ".claude" / "sessions"
    registry.mkdir(parents=True, exist_ok=True)
    (registry / "100.json").write_text(json.dumps({
        "pid": pid if pid is not None else os.getpid(), "sessionId": session_id, "cwd": cwd,
        "startedAt": 1788151132455, "status": "busy",
    }))
    trail = home / ".claude" / "projects" / "-some-slug" / f"{session_id}.jsonl"
    write_jsonl(trail, records)
    return trail


def claude_inventory(session_id: str, cwd: str, status: str = "busy") -> list[dict]:
    return [{
        "sessionId": session_id,
        "cwd": cwd,
        "startedAt": 1788151132455,
        "status": status,
    }]


def claude_tool_use(name: str, call_id: str, **arguments) -> dict:
    return {"type": "assistant", "timestamp": "2026-08-31T04:44:00.000Z", "message": {
        "role": "assistant", "stop_reason": "tool_use",
        "content": [{"type": "tool_use", "id": call_id, "name": name, "input": arguments}]}}


def claude_tool_result(call_id: str) -> dict:
    return {"type": "user", "timestamp": "2026-08-31T04:44:05.000Z", "message": {
        "role": "user", "content": [{"type": "tool_result", "tool_use_id": call_id, "content": "secret output"}]}}


HELD_LOCKS: list[int] = []  # descriptors kept open so a test's Codex thread counts as running


def codex_home(
    home: Path, session_id: str, cwd: str, records: list, parent: str | None = None, held: bool = True
) -> Path:
    """A Codex thread on disk; `held` takes its writer lock the way a live thread does (flock is per descriptor)."""
    locks = home / ".codex" / "thread-writer-locks"
    locks.mkdir(parents=True, exist_ok=True)
    (locks / ".coordination.lock").write_text("")
    (locks / f"{session_id}.lock").write_text("")
    if held:
        holder = os.open(locks / f"{session_id}.lock", os.O_RDWR)
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        HELD_LOCKS.append(holder)
    meta = {"session_id": session_id, "id": session_id, "timestamp": "2026-08-31T01:49:03.193Z", "cwd": cwd}
    if parent:
        meta["parent_thread_id"] = parent
        meta["thread_source"] = "subagent"
    trail = home / ".codex" / "sessions" / "2026" / "08" / "31" / f"rollout-2026-08-31T01-49-03-{session_id}.jsonl"
    write_jsonl(trail, [{"timestamp": "2026-08-31T01:50:04.086Z", "type": "session_meta", "payload": meta}] + records)
    return trail


class ClaudeInventoryTests(unittest.TestCase):
    def test_claude_inventory_reader_validates_the_cli_response(self):
        valid = subprocess.CompletedProcess(
            ["claude", "agents", "--json"], 0,
            json.dumps([{"sessionId": "one"}, "ignored"]), "",
        )
        with mock.patch.object(pulse, "run", return_value=valid):
            self.assertEqual(pulse.read_claude_inventory(), [{"sessionId": "one"}])
        for result in (
            subprocess.CompletedProcess(["claude"], 1, "", "unavailable"),
            subprocess.CompletedProcess(["claude"], 0, "not json", ""),
            subprocess.CompletedProcess(["claude"], 0, "{}", ""),
        ):
            with mock.patch.object(pulse, "run", return_value=result):
                self.assertIsNone(pulse.read_claude_inventory())


class AgentCollectorTests(unittest.TestCase):
    def setUp(self):
        """Keep the machine's own live sessions out of the suite.

        `AgentCollector.scan()` asks Claude Code which sessions are running, so
        without this every test here would see whatever the developer happens to
        have open and fail differently on every machine. Tests that care about
        the inventory patch it again with their own value.
        """
        patcher = mock.patch.object(pulse, "read_claude_inventory", return_value=[])
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_claude_inventory_is_not_restarted_on_every_draw(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            collector = pulse.AgentCollector(home, home / "Projects")
            with mock.patch.object(pulse, "read_claude_inventory", return_value=[]) as reader:
                collector.scan()
                collector.scan()
            reader.assert_called_once_with()

    def test_claude_inventory_resolves_the_matching_trail(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            root = home / "Projects"
            trail = claude_home(home, "sess-1", str(root / "Example" / "app"), [
                claude_tool_use("Edit", "toolu_1", file_path=str(root / "Example" / "app" / "main.swift"), old_string="x"),
                claude_tool_use("AskUserQuestion", "toolu_2", questions=[{"question": "Ship it?"}]),
            ])
            with mock.patch.object(pulse, "read_claude_inventory", return_value=claude_inventory("sess-1", str(root / "Example" / "app"))):
                observations = pulse.AgentCollector(home, root).scan(datetime(2026, 8, 31, tzinfo=timezone.utc))
        self.assertEqual([o.provider for o in observations], ["claude"])
        session = observations[0]
        self.assertEqual((session.session_id, session.project, session.trail, session.status), ("sess-1", "Example", trail, "busy"))
        self.assertEqual(session.started_at, datetime.fromtimestamp(1788151132.455, tz=timezone.utc))
        self.assertEqual([(e.kind, e.label, e.key) for e in session.events],
                         [("edit", "main.swift", None), ("question", "AskUserQuestion", "toolu_2")])
        self.assertNotIn("Ship it?", repr(session.events))

    def test_claude_registry_entry_not_in_inventory_is_not_live(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            claude_home(home, "sess-1", "/tmp", [claude_tool_use("Bash", "t1", command="rm -rf")], pid=os.getpid())
            with mock.patch.object(pulse, "read_claude_inventory", return_value=[]):
                self.assertEqual(pulse.AgentCollector(home, home / "Projects").scan(), [])

    def test_claude_inventory_failure_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            claude_home(home, "sess-1", "/tmp", [claude_tool_use("Bash", "t1", command="rm -rf")], pid=os.getpid())
            with mock.patch.object(pulse, "read_claude_inventory", return_value=None):
                self.assertEqual(pulse.AgentCollector(home, home / "Projects").scan(), [])

    def test_codex_lock_nobody_holds_is_not_live_however_fresh_the_rollout(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            root = home / "Projects"
            codex_home(home, "thread-1", str(root / "Example"), [], held=False)
            self.assertEqual(pulse.AgentCollector(home, root).scan(), [])

    def test_codex_held_lock_is_live_however_quiet_the_rollout(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            root = home / "Projects"
            trail = codex_home(home, "thread-1", str(root / "Example"), [], held=False)
            lock = home / ".codex" / "thread-writer-locks" / "thread-1.lock"
            quiet = datetime(2026, 8, 31, tzinfo=timezone.utc).timestamp() - 3600
            os.utime(trail, (quiet, quiet))
            holder = os.open(lock, os.O_RDWR)  # a second open file description contends like another process
            try:
                fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.assertTrue(pulse.codex_lock_held(lock))
                self.assertEqual([o.session_id for o in pulse.AgentCollector(home, root).scan()], ["thread-1"])
            finally:
                os.close(holder)
            self.assertFalse(pulse.codex_lock_held(lock))
            self.assertEqual(pulse.AgentCollector(home, root).scan(), [])

    def test_codex_writer_lock_resolves_index_and_rollout(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            root = home / "Projects"
            index = home / ".codex" / "session_index.jsonl"
            index.parent.mkdir(parents=True)
            index.write_text(
                json.dumps({"id": "thread-1", "thread_name": "old name", "updated_at": "2026-08-30T21:00:00Z"}) + "\n"
                + json.dumps({"id": "thread-1", "thread_name": "new name", "updated_at": "2026-08-30T22:00:00Z"}) + "\n"
            )
            trail = codex_home(home, "thread-1", str(root), [
                {"timestamp": "2026-08-31T01:50:13.865Z", "type": "response_item", "payload": {
                    "type": "function_call", "name": "request_user_input", "call_id": "call_1",
                    "arguments": "{\"questions\": [\"private\"]}"}},
                {"timestamp": "2026-08-31T01:50:20.000Z", "type": "event_msg", "payload": {
                    "type": "item_completed", "item": {"type": "FileChange", "changes": {str(root / "x" / "notes.md"): {"type": "add"}}}}},
            ])
            codex_home(home, "thread-2", str(root / "Example"), [], parent="thread-1")
            observations = pulse.AgentCollector(home, root).scan()
            self.assertEqual(pulse.codex_session_index(home)["thread-1"]["thread_name"], "new name")
        by_id = {o.session_id: o for o in observations}
        self.assertEqual(set(by_id), {"thread-1", "thread-2"})
        parent = by_id["thread-1"]
        self.assertEqual((parent.provider, parent.project, parent.trail, parent.parent_id), ("codex", "Projects", trail, None))
        self.assertEqual(parent.started_at, datetime(2026, 8, 31, 1, 49, 3, 193000, tzinfo=timezone.utc))
        self.assertEqual([(e.kind, e.label, e.key) for e in parent.events],
                         [("question", "request_user_input", "call_1"), ("edit", "notes.md", None)])
        self.assertEqual((by_id["thread-2"].parent_id, by_id["thread-2"].project), ("thread-1", "Example"))
        self.assertNotIn("private", repr(observations))

    def test_incremental_cursor_reads_only_appended_complete_records(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            trail = claude_home(home, "sess-1", "/tmp/work", [claude_tool_use("Read", "t1", file_path="/tmp/a")])
            collector = pulse.AgentCollector(home, home / "Projects")
            with mock.patch.object(pulse, "read_claude_inventory", return_value=claude_inventory("sess-1", "/tmp/work")):
                first = collector.scan()
                self.assertEqual([e.label for e in first[0].events], ["Read"])
                cursor = collector.cursors["claude:sess-1"]
                offset = cursor.offset
                with mock.patch.object(pulse.TrailCursor, "read_new_records", wraps=cursor.read_new_records) as reads:
                    self.assertEqual(collector.scan()[0].events, [])
                self.assertEqual(cursor.offset, offset)
                write_jsonl(trail, [claude_tool_use("Grep", "t2", pattern="x")], partial='{"type": "assistant", "mess')
                second = collector.scan()
                self.assertEqual([e.label for e in second[0].events], ["Grep"])
                self.assertEqual(cursor.offset, trail.stat().st_size - len('{"type": "assistant", "mess'))
                with trail.open("a") as handle:
                    handle.write('age": {"role": "assistant", "stop_reason": "end_turn", "content": []}}\n')
                self.assertEqual([e.kind for e in collector.scan()[0].events], ["turn_end"])
                self.assertEqual(cursor.offset, trail.stat().st_size)

    def test_missing_and_malformed_provider_data_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            root = home / "Projects"
            self.assertEqual(pulse.AgentCollector(home, root).scan(), [])
            registry = home / ".claude" / "sessions"
            registry.mkdir(parents=True)
            (registry / "1.json").write_text("{not json")
            (registry / "2.json").write_text(json.dumps({"sessionId": "no-pid"}))
            (registry / "3.json").write_text(json.dumps({"pid": os.getpid(), "sessionId": "no-trail", "cwd": 7}))
            trail = claude_home(home, "sess-1", "relative/odd", [])
            trail.write_text('{"type": "assistant", "message": "not a dict"}\n\x00garbage\n{"type": "user", "message": {"content": [{"type": "tool_result"}]}}\n')
            locks = home / ".codex" / "thread-writer-locks"
            locks.mkdir(parents=True)
            (locks / "orphan.lock").write_text("")
            rollout = home / ".codex" / "sessions" / "2026" / "08" / "31" / "rollout-x-broken.jsonl"
            rollout.parent.mkdir(parents=True)
            rollout.write_text("garbage first line\n")
            (locks / "broken.lock").write_text("")
            inventory = [
                *claude_inventory("no-trail", 7),
                *claude_inventory("sess-1", "relative/odd"),
            ]
            with mock.patch.object(pulse, "read_claude_inventory", return_value=inventory):
                observations = pulse.AgentCollector(home, root).scan()
        by_id = {o.session_id: o for o in observations}
        self.assertEqual(set(by_id), {"no-trail", "sess-1"})
        self.assertIsNone(by_id["no-trail"].trail)
        self.assertEqual(by_id["no-trail"].project, "?")
        self.assertEqual(by_id["sess-1"].project, "odd")
        self.assertEqual([(e.kind, e.key) for e in by_id["sess-1"].events], [("activity", None), ("answer", None)])


T0 = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def event(kind: str, seconds: float, label: str = "", key: str | None = None) -> "pulse.AgentEvent":
    return pulse.AgentEvent(at(seconds), kind, label, key)


def observation(provider: str, session_id: str, started: float = 0, events=(), parent: str | None = None):
    return pulse.AgentObservation(
        provider, session_id, "Projects", at(started), None, parent, None, list(events)
    )


class AgentStateTests(unittest.TestCase):
    def test_claude_question_waits_until_its_matching_result(self):
        ledger = pulse.AgentLedger(at(0))
        ledger.apply(event("question", 1, "AskUserQuestion", "toolu_q"))
        self.assertEqual((ledger.state, ledger.state_since), ("waiting", at(1)))
        ledger.apply(event("activity", 2, "Bash"))
        ledger.apply(event("edit", 3, "pulse"))
        ledger.apply(event("turn_end", 4))
        ledger.apply(event("answer", 5, "", "toolu_other"))
        ledger.settle(at(3600))
        self.assertEqual((ledger.state, ledger.state_since), ("waiting", at(1)))
        ledger.apply(event("answer", 3601, "", "toolu_q"))
        self.assertEqual((ledger.state, ledger.state_since), ("thinking", at(3601)))

    def test_codex_input_and_escalated_approval_wait_until_output(self):
        ledger = pulse.AgentLedger(at(0))
        ledger.apply(event("question", 1, "request_user_input", "call_1"))
        self.assertEqual(ledger.state, "waiting")
        ledger.apply(event("answer", 2, "", "call_1"))
        self.assertEqual(ledger.state, "thinking")
        for record in (
            {"timestamp": "2026-08-31T12:00:03Z", "type": "event_msg",
             "payload": {"type": "exec_approval_request", "call_id": "call_2", "command": ["rm", "-rf", "/"]}},
        ):
            for item in pulse.codex_events(record, at(3)):
                ledger.apply(item)
        self.assertEqual((ledger.state, ledger.activity), ("waiting", "exec_approval_request"))
        self.assertNotIn("rm", ledger.activity)
        ledger.apply(event("activity", 4, "reasoning"))
        self.assertEqual(ledger.state, "waiting")
        for item in pulse.codex_events(
            {"timestamp": "2026-08-31T12:00:05Z", "type": "event_msg",
             "payload": {"type": "exec_command_begin", "call_id": "call_2"}}, at(5)
        ):
            ledger.apply(item)
        self.assertEqual((ledger.state, ledger.state_since), ("thinking", at(5)))

    def test_silence_is_still_not_waiting(self):
        ledger = pulse.AgentLedger(at(0))
        ledger.settle(at(7200))
        self.assertEqual((ledger.state, ledger.state_since), ("still", at(0)))
        ledger.apply(event("activity", 10, "Read"))
        ledger.settle(at(7200))
        self.assertEqual(ledger.state, "thinking")  # silence changes nothing on its own
        band = pulse.AgentBand()
        band.update([observation("claude", "a", events=[event("activity", 1, "prompt")])], at(1))
        band.update([observation("claude", "a")], at(9000))
        self.assertEqual(band.positions[0].session.state, "thinking")

    def test_mutations_edit_and_other_activity_thinks(self):
        ledger = pulse.AgentLedger(at(0))
        ledger.apply(event("edit", 1, "pulse"))
        self.assertEqual((ledger.state, ledger.state_since, ledger.activity), ("editing", at(1), "pulse"))
        ledger.apply(event("edit", 2, "README.md"))
        self.assertEqual((ledger.state, ledger.state_since, ledger.activity), ("editing", at(1), "README.md"))
        for label, second in (("Read", 3), ("Grep", 4), ("Bash", 5), ("reasoning", 6), ("prompt", 7), ("command", 8)):
            ledger.apply(event("activity", second, label))
            self.assertEqual((ledger.state, ledger.activity), ("thinking", label))
        self.assertEqual(ledger.state_since, at(3))
        ledger.apply(event("edit", 9, "patch"))
        self.assertEqual((ledger.state, ledger.state_since), ("editing", at(9)))

    def test_finishing_lasts_exactly_two_minutes(self):
        ledger = pulse.AgentLedger(at(0))
        ledger.apply(event("activity", 1, "Bash"))
        ledger.apply(event("turn_end", 10))
        self.assertEqual((ledger.state, ledger.state_since), ("finishing", at(10)))
        ledger.settle(at(129.9))
        self.assertEqual(ledger.state, "finishing")
        ledger.settle(at(130))
        self.assertEqual((ledger.state, ledger.state_since, ledger.activity), ("still", at(130), ""))
        ledger.apply(event("turn_end", 200))
        ledger.apply(event("turn_end", 250))  # a second end marker does not restart the clock
        self.assertEqual(ledger.state_since, at(200))

    def test_subagents_roll_up_into_the_parent(self):
        band = pulse.AgentBand()
        band.update([
            observation("claude", "parent", 0, [event("subagent_start", 1, "Agent", "toolu_a"), event("subagent_start", 2, "Agent", "toolu_b")]),
            observation("codex", "root", 5, [event("activity", 6, "exec")]),
            observation("codex", "child-1", 7, [event("question", 8, "request_user_input", "c1")], parent="root"),
            observation("codex", "child-2", 8, [event("edit", 9, "x.py")], parent="root"),
        ], at(10))
        strips = [strip.session for strip in band.positions if strip]
        self.assertEqual([(s.session_id, s.subagents, s.state) for s in strips],
                         [("parent", 2, "thinking"), ("root", 2, "thinking")])
        self.assertEqual(band.queued, 0)
        band.update([
            observation("claude", "parent", 0, [event("answer", 11, "", "toolu_a")]),
            observation("codex", "root", 5),
            observation("codex", "child-2", 8, parent="root"),
        ], at(12))
        strips = [strip.session for strip in band.positions if strip]
        self.assertEqual([(s.session_id, s.subagents) for s in strips], [("parent", 1), ("root", 1)])


class AgentBandSlotTests(unittest.TestCase):
    def eight(self):
        return [observation("claude" if index % 2 else "codex", name, index) for index, name in enumerate("ABCDEFGH")]

    def occupants(self, band):
        return [strip.session.session_id if strip else None for strip in band.positions]

    def test_first_six_sessions_keep_their_positions(self):
        band = pulse.AgentBand()
        band.update(list(reversed(self.eight())), at(10))
        self.assertEqual(self.occupants(band), list("ABCDEF"))
        self.assertEqual((band.queue, band.queued), (["codex:G", "claude:H"], 2))
        band.update(self.eight(), at(11))
        self.assertEqual(self.occupants(band), list("ABCDEF"))
        self.assertEqual(band.queued, 2)

    def test_oldest_queued_session_takes_the_exact_vacated_position(self):
        band = pulse.AgentBand()
        band.update(self.eight(), at(10))
        without_c = [o for o in self.eight() if o.session_id != "C"]
        band.update(without_c, at(20))
        self.assertEqual(self.occupants(band), list("ABCDEF"))
        self.assertTrue(band.positions[2].fading)
        band.update(without_c, at(24.9))
        self.assertEqual(self.occupants(band), list("ABCDEF"))
        band.update(without_c, at(25))
        self.assertEqual(self.occupants(band), list("ABGDEF"))
        self.assertEqual((band.queue, band.queued), (["claude:H"], 1))
        self.assertFalse(band.positions[2].fading)

    def test_closed_queued_sessions_are_skipped(self):
        band = pulse.AgentBand()
        band.update(self.eight(), at(10))
        remaining = [o for o in self.eight() if o.session_id not in ("A", "G")]
        band.update(remaining, at(20))
        self.assertEqual(band.queue, ["claude:H"])
        band.update(remaining, at(25))
        self.assertEqual(self.occupants(band), list("HBCDEF"))
        self.assertEqual(band.queued, 0)
        returned = remaining + [observation("claude", "G", 6)]
        band.update(returned, at(26))
        self.assertEqual(band.queue, ["claude:G"])  # back of the line, not its old place

    def test_last_departure_collapses_after_fade(self):
        band = pulse.AgentBand()
        self.assertFalse(band.active)
        band.update([observation("claude", "only", 0, [event("activity", 1, "Bash")])], at(1))
        self.assertTrue(band.active)
        band.update([], at(10))
        self.assertTrue(band.active)
        band.update([], at(14.9))
        self.assertTrue(band.active)
        band.update([], at(15))
        self.assertFalse(band.active)
        self.assertEqual((band.ledgers, band.sessions), ({}, {}))
        # A finishing session keeps its position for the full celebration even
        # while its provider record remains live. Once it is gone, the normal
        # five-second fade starts immediately.
        band.update([observation("codex", "done", 0, [event("turn_end", 100)])], at(100))
        band.update([], at(110))
        self.assertEqual((band.active, band.positions[0].session.state, band.positions[0].fading), (True, "finishing", True))
        band.update([], at(114.9))
        self.assertTrue(band.active)
        band.update([], at(115))
        self.assertFalse(band.active)


ANSI = __import__("re").compile(r"\x1b\[[0-9;]*m")


def strip_session(provider: str, name: str, state: str, project: str = "Example", activity: str = "Bash", subagents: int = 0):
    return pulse.AgentStrip(pulse.AgentSession(provider, name, project, T0, state, T0, activity, subagents))


def band_with(states: list[str]) -> "pulse.AgentBand":
    band = pulse.AgentBand()
    for index, state in enumerate(states):
        band.positions[index] = strip_session("claude" if index % 2 else "codex", f"s{index}", state)
    return band


class AgentBandRenderingTests(unittest.TestCase):
    def test_every_icon_frame_is_exactly_seven_by_three(self):
        self.assertEqual(set(pulse.AGENT_ICON_FRAMES), set(pulse.AGENT_STATES))
        for state, frames in pulse.AGENT_ICON_FRAMES.items():
            self.assertEqual(len(frames), 3, state)
            for frame in frames:
                self.assertEqual(len(frame), 3, state)
                self.assertEqual([len(row) for row in frame], [7, 7, 7], state)
        self.assertEqual([len(row) for row in pulse.AGENT_ICON_EMPTY], [7, 7, 7])
        self.assertEqual(pulse.agent_icon_frame("unknown", 1), pulse.AGENT_ICON_FRAMES["still"][1])

    def test_all_five_states_render_the_requested_frames(self):
        band = band_with(["still", "thinking", "editing", "finishing", "waiting"])
        expected = {
            "still": ("▌ ▐", "▌ ▐", "▌ ▐"),
            "thinking": ("·  ", "·· ", "···"),
            "editing": ("≡╱◆", "◆╲≡", "≡╱◆"),
            "finishing": ("·✓·", "✦✓✦", "·✓·"),
            "waiting": (">> ", " > ", ">> "),
        }
        width = pulse.agent_strip_width(111)
        inset = (width - 7) // 2
        for frame in range(3):
            rows = pulse.agent_band_rows(band, pulse.Palette(False), 111, frame, at(30))
            middle = rows[3][1 : 1 + 6 * width]
            for index, state in enumerate(["still", "thinking", "editing", "finishing", "waiting"]):
                cell = middle[index * width : (index + 1) * width]
                self.assertEqual(cell[inset : inset + 7], f"│ {expected[state][frame]} │", (state, frame))
        self.assertIn("0:30", rows[6])
        self.assertNotIn("WAITING", rows[6])  # the icon carries the state; the row is time only

    def test_six_positions_have_equal_width_at_supported_dashboard_sizes(self):
        band = band_with(["thinking"] * 6)
        for width in (64, 111, 132):
            strip = pulse.agent_strip_width(width)
            self.assertGreaterEqual(strip, 9)
            rows = pulse.agent_band_rows(band, pulse.Palette(False), width, 0, at(1))
            self.assertEqual(len(rows), 8)
            for row in rows:
                self.assertLessEqual(len(row), width, width)
            inset = (strip - 7) // 2
            top = rows[2][1 : 1 + 6 * strip]
            self.assertEqual([top[index * strip + inset : index * strip + inset + 7] for index in range(6)], ["╭─────╮"] * 6, width)
            self.assertEqual(len(top), strip * 6)

    def test_empty_positions_keep_the_static_guard(self):
        two = band_with(["editing", "waiting"])
        six = band_with(["editing"] * 6)
        width = pulse.agent_strip_width(111)
        inset = (width - 7) // 2
        rows_two = pulse.agent_band_rows(two, pulse.Palette(False), 111, 0, at(1))
        rows_six = pulse.agent_band_rows(six, pulse.Palette(False), 111, 0, at(1))
        for index in range(2, 6):
            start = 1 + index * width + inset
            self.assertEqual(rows_two[2][start : start + 7], "╭─────╮")
            self.assertEqual(rows_two[3][start : start + 7], "│     │")
            self.assertEqual(rows_two[4][start : start + 7], "╰─────╯")
            for row in (1, 5, 6, 7):
                self.assertEqual(rows_two[row][1 + index * width : 1 + (index + 1) * width].strip(), "")
        self.assertEqual(rows_two[2][: 1 + width], rows_six[2][: 1 + width])  # occupied strips are not stretched
        self.assertEqual(len(rows_two[2]), len(rows_six[2]))

    def test_overflow_count_lives_in_the_header(self):
        band = pulse.AgentBand()
        band.update([observation("claude", name, index) for index, name in enumerate("ABCDEFGH")], at(10))
        rows = pulse.agent_band_rows(band, pulse.Palette(False), 111, 0, at(10))
        self.assertIn("06 SESSIONS / +2 QUEUED", rows[0])
        self.assertTrue(rows[0].startswith("┌─ // LIVE AGENTS"))
        self.assertFalse(any("QUEUED" in row or "G" in row.replace("AGENTS", "") for row in rows[1:]))

    def test_all_empty_positions_collapse_to_one_dim_line(self):
        empty = pulse.AgentBand()
        rows = pulse.agent_band_rows(empty, pulse.Palette(False), 111, 0, at(1))
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0].startswith("// LIVE AGENTS"))
        self.assertIn("STANDBY // 00 SESSIONS", rows[0])
        self.assertEqual(pulse.agent_band_height(empty), 1)
        self.assertEqual(pulse.agent_band_height(band_with(["still"])), 8)
        painted = pulse.agent_band_rows(empty, pulse.Palette(True), 111, 0, at(1))[0]
        self.assertTrue(painted.startswith("\x1b[38;2;116;120;132m"))  # the dashboard's muted ink

    def test_metadata_is_clipped_without_exposing_raw_activity(self):
        record = claude_tool_use("Edit", "toolu_1", file_path="/Users/someone/Secret Project/deeply/nested/main.swift", old_string="password = 'hunter2'", new_string="x")
        prompt = {"type": "user", "timestamp": "2026-08-31T12:00:00Z", "message": {"role": "user", "content": "please fix the login bug quietly"}}
        band = pulse.AgentBand()
        events = [e for r in (prompt, record) for e in pulse.claude_events(r, at(0))]
        band.update([pulse.AgentObservation("claude", "s", "AVeryLongProjectNameThatKeepsGoing", at(0), None, None, None, events)], at(5))
        for width in (64, 111):
            rows = pulse.agent_band_rows(band, pulse.Palette(False), width, 0, at(5))
            text = "\n".join(rows)
            for secret in ("hunter2", "login bug", "/Users", "Secret Project", "nested"):
                self.assertNotIn(secret, text)
            self.assertIn("main.swi", rows[7])  # the basename survives, clipped to the cell
            self.assertIn("…", rows[5])
            cell = pulse.agent_strip_width(width)
            # Rails live outside the six cells; clipping still happens inside
            # the occupied cell rather than exposing the raw path.
            self.assertLessEqual(len(rows[5][1 : 1 + cell].rstrip()), cell)

    def test_the_band_uses_neutral_signal_ink_and_reserves_pink_for_attention(self):
        band = band_with(["still", "thinking", "editing", "finishing", "waiting"])
        text = "\n".join(pulse.agent_band_rows(band, pulse.Palette(True), 111, 0, at(9)))
        for dashboard_color in ("211;210;216", "116;120;132"):
            self.assertIn(dashboard_color, text)
        self.assertEqual(text.count("250;84;185"), 3)  # the waiting icon's three rows
        self.assertNotIn("155;104;255", text)  # finishing is motion, not a color state
        self.assertNotIn("26;153;123", text)  # editing is motion, not a color state
        self.assertNotIn("38;2;0;120;190", text)  # the discarded riso blue
        self.assertNotIn("48;2;", text)  # never a background over a translucent terminal
        header = pulse.agent_band_rows(band, pulse.Palette(False), 111, 0, at(9))[0]
        self.assertTrue(header.startswith("┌─ // LIVE AGENTS"))
        self.assertTrue(header.endswith("AGT/01 // 05 SESSIONS / 01 NEEDS YOU ─┐"))
        self.assertNotIn("░", header)
        self.assertEqual(len(header), 111)

    def test_narrow_windows_reflow_full_cards_without_a_frame(self):
        band = band_with(["still", "thinking", "editing", "finishing", "waiting"])
        band.positions[3].session.subagents = 2
        for width, columns in ((60, 6), (50, 5), (40, 4), (30, 3), (20, 2)):
            rows = pulse.agent_band_rows(
                band, pulse.Palette(False), width, 1, at(30), columns=columns, framed=False
            )
            self.assertEqual(len(rows), 8, width)
            self.assertIn("LIVE", rows[0])
            self.assertFalse(rows[0].startswith("┌"))
            self.assertLessEqual(max(len(row) for row in rows), width)
            self.assertIn("CLAUDE", rows[1])
            self.assertIn("··", rows[3])  # frame one of thinking
            if columns >= 3:
                self.assertIn("◆╲≡", rows[3])
            if columns >= 5:
                self.assertIn(">", rows[3])
            self.assertIn("Example", "\n".join(rows))
            self.assertIn("0:30", "\n".join(rows))
            self.assertIn("Bash", "\n".join(rows))
        colored = pulse.agent_band_rows(band, pulse.Palette(True), 60, 1, at(30), columns=6, framed=False)
        plain = pulse.agent_band_rows(band, pulse.Palette(False), 60, 1, at(30), columns=6, framed=False)
        self.assertEqual([ANSI.sub("", row) for row in colored], plain)
        self.assertEqual("\n".join(colored).count("250;84;185"), 3)

    def test_frame_only_appears_at_wide_widths(self):
        band = band_with(["thinking"] * 6)
        for width in (70, 111, 132):
            strip = pulse.agent_strip_width(width, 6, True)
            rows = pulse.agent_band_rows(band, pulse.Palette(False), width, 0, at(9), framed=True)
            self.assertTrue(rows[0].startswith("┌─"))
            self.assertTrue(rows[0].endswith("┐"))
            self.assertEqual(rows[1][0], "│")
            self.assertEqual(rows[-1][0], "│")
            self.assertEqual(rows[2][1 : 1 + 6 * strip].count("╭─────╮"), 6)
            self.assertLessEqual(max(map(len, rows)), width)
            self.assertEqual(rows[1][-1], "│")
            self.assertEqual(rows[-1][-1], "│")
        rows = pulse.agent_band_rows(band, pulse.Palette(False), 60, 0, at(9), columns=6, framed=False)
        self.assertTrue(rows[0].startswith("// LIVE AGENTS"))
        self.assertNotIn("│", rows[1][0])

    def test_extra_sessions_fill_identical_rows_before_the_queue(self):
        band = pulse.AgentBand()
        band.update([observation("claude", str(index), index) for index in range(8)], at(10))
        rows = pulse.agent_band_rows(band, pulse.Palette(False), 70, 0, at(10), columns=6, grid_rows=2)
        self.assertEqual(len(rows), 15)
        self.assertIn("08 SESSIONS", rows[0])
        self.assertNotIn("QUEUED", rows[0])
        self.assertEqual(rows[8][0], "│")  # provider row of the second full card row
        self.assertIn("CLAUDE", rows[8])
        self.assertRegex(rows[13], r"\b0:0[0-9]\b")

    def test_still_cards_breathe_but_the_instrument_remains_foreground_only(self):
        band = band_with(["still"])
        early = "\n".join(pulse.agent_band_rows(band, pulse.Palette(True), 111, 0, at(9)))
        later = "\n".join(pulse.agent_band_rows(band, pulse.Palette(True), 111, 3, at(9)))
        self.assertNotEqual(early, later)
        self.assertNotIn("26;153;123", early)
        self.assertNotIn("155;104;255", early)
        self.assertNotIn("48;2;", early + later)

    def test_waiting_cards_are_the_only_ones_that_call_for_attention(self):
        band = band_with(["thinking", "waiting"])
        plain = pulse.agent_band_rows(band, pulse.Palette(False), 111, 0, at(9))
        colored = pulse.agent_band_rows(band, pulse.Palette(True), 111, 0, at(9))
        self.assertIn("01 NEEDS YOU", plain[0])
        self.assertEqual("\n".join(colored).count("250;84;185"), 3)

    def test_agents_frame_reflows_columns_and_prioritizes_full_rows(self):
        band = band_with(["thinking"] * 6)
        self.assertEqual([pulse.agent_grid_columns(width) for width in (60, 50, 40, 30, 20)], [6, 5, 4, 3, 2])
        cases = ((20, 8, 8), (19, 20, 1), (60, 7, 1), (60, 8, 8), (70, 13, 13))
        for columns, lines, expected_rows in cases:
            rows = pulse.agents_frame_rows(band, pulse.Palette(False), columns, lines, 0, at(30))
            self.assertEqual(len(rows), expected_rows, (columns, lines))
            self.assertLessEqual(max(len(row) for row in rows), columns, (columns, lines))
        overflow = pulse.AgentBand()
        overflow.update([observation("claude", str(index), index) for index in range(8)], at(10))
        rows = pulse.agents_frame_rows(overflow, pulse.Palette(False), 60, 19, 0, at(10))
        self.assertEqual(len(rows), 15)  # masthead yields so row two can fit
        self.assertNotIn("PROJECT PULSE", "\n".join(rows))

    def test_no_color_keeps_the_same_unicode_geometry(self):
        band = band_with(["still", "thinking", "editing", "finishing", "waiting"])
        band.positions[2].closed_at = at(3)
        for width in (64, 111, 132):
            plain = pulse.agent_band_rows(band, pulse.Palette(False), width, 1, at(9))
            colored = pulse.agent_band_rows(band, pulse.Palette(True), width, 1, at(9))
            self.assertEqual([ANSI.sub("", row) for row in colored], plain)
            self.assertNotEqual(colored, plain)

    def test_under_the_floor_the_pane_shows_the_wordmark_and_a_notice(self):
        github = pulse.GithubSnapshot(False)
        slots = [pulse.Slot(1, recommendation("Alpha"))]
        for columns, height in ((100, 19), (59, 40)):
            output = io.StringIO()
            with mock.patch.object(pulse, "terminal_columns", return_value=columns):
                with mock.patch.object(pulse, "dashboard_width", return_value=max(60, columns)):
                    with mock.patch.object(pulse, "dashboard_height", return_value=height):
                        with redirect_stdout(output):
                            pulse.print_live_dashboard(
                                Path("/workspace"), [], [], github, slots, pulse.Palette(False),
                                None, True, slots[0].item.id, True, None, None,
                            )
            lines = output.getvalue().splitlines()
            self.assertEqual(len(lines), 5, (columns, height))
            self.assertIn("▄", lines[0])  # the wordmark
            self.assertEqual(lines[-1].strip(), "MAKE THE WINDOW 60x20 OR LARGER")
            self.assertLessEqual(max(len(line) for line in lines), columns)
            self.assertNotIn("WORKSPACE", output.getvalue())

    def test_at_the_floor_one_move_still_fits_without_wrapping(self):
        github = pulse.GithubSnapshot(True, user="demo-user")
        slots = [pulse.Slot(index, recommendation(str(index))) for index in range(1, 6)]
        ideas = [pulse.Slot(index, pulse.idea_recommendation(idea("tech", f"Idea {index}"))) for index in range(6, 11)]

        def render(columns, height):
            output = io.StringIO()
            with mock.patch.object(pulse, "terminal_columns", return_value=columns):
                with mock.patch.object(pulse, "dashboard_width", return_value=columns):
                    with mock.patch.object(pulse, "dashboard_height", return_value=height):
                        with redirect_stdout(output):
                            pulse.print_live_dashboard(
                                Path("/workspace"), [], [], github, slots, pulse.Palette(False),
                                None, True, slots[0].item.id, True, ideas, None,
                            )
            return output.getvalue().splitlines()

        lines = render(60, 20)
        text = "\n".join(lines)
        self.assertLessEqual(len(lines), 20)
        self.assertLessEqual(max(len(line) for line in lines), 60)
        self.assertTrue(lines[0].startswith("PROJECT PULSE //"))
        self.assertIn("▄", lines[1])
        self.assertIn("WORKSPACE /", text)
        self.assertIn("Handle 1", text)  # one move fits at the floor
        self.assertNotIn("Handle 2", text)
        self.assertNotIn("LIVE AGENTS", text)
        self.assertIn("05 IDEAS / TERMINAL TOO SHORT", text)
        self.assertIn("AGENT PIPELINES", text)
        # Anything smaller is not a dashboard: the notice.
        lines = render(60, 19)
        self.assertEqual(lines[-1].strip(), "MAKE THE WINDOW 60x20 OR LARGER")
        # The 63x28 window that wrapped before: real width, no wrapping, several moves.
        lines = render(63, 28)
        self.assertLessEqual(len(lines), 28)
        self.assertLessEqual(max(len(line) for line in lines), 63)
        self.assertIn("Handle 1", "\n".join(lines))
        self.assertTrue(any(len(line) == 63 for line in lines))  # lines are cut to 63, not 64

    def test_the_notice_also_covers_a_layout_that_would_not_fit_above_the_nominal_floor(self):
        # The floor is nominal; what decides is the smallest layout's exact row count.
        github = pulse.GithubSnapshot(False)
        slots = [pulse.Slot(1, recommendation("Alpha"))]
        ideas = [pulse.Slot(2, pulse.idea_recommendation(idea("tech", "Idea")))]

        def render(height, segments):
            output = io.StringIO()
            with mock.patch.object(pulse, "instrument_segments", return_value=segments):
                with mock.patch.object(pulse, "terminal_columns", return_value=60):
                    with mock.patch.object(pulse, "dashboard_width", return_value=60):
                        with mock.patch.object(pulse, "dashboard_height", return_value=height):
                            with redirect_stdout(output):
                                pulse.print_live_dashboard(
                                    Path("/workspace"), [], [], github, slots, pulse.Palette(False),
                                    None, True, slots[0].item.id, True, ideas, None,
                                )
            return output.getvalue().splitlines()

        wrapped = ["X" * 50] * 7  # seven instrument lines: far more than any real workspace at 60 columns
        lines = render(20, wrapped)  # at the nominal floor, yet 21 rows would be needed
        self.assertIn("MAKE THE WINDOW 60x20 OR LARGER", "\n".join(lines))
        self.assertNotIn("WORKSPACE", "\n".join(lines))
        lines = render(21, wrapped)
        self.assertEqual(len(lines), 21)  # exactly the terminal's rows, never a scroll
        self.assertIn("01 SIGNALS / TERMINAL TOO SHORT", "\n".join(lines))  # the move folded
        self.assertNotIn("Handle Alpha", "\n".join(lines))

    def test_dashboard_width_follows_the_terminal_down_to_the_floor(self):
        size = os.terminal_size
        with mock.patch.object(pulse.shutil, "get_terminal_size", return_value=size((61, 30))):
            self.assertEqual(pulse.dashboard_width(), 61)
        with mock.patch.object(pulse.shutil, "get_terminal_size", return_value=size((40, 30))):
            self.assertEqual(pulse.dashboard_width(), 60)
            self.assertTrue(pulse.viewport_below_floor())
        with mock.patch.object(pulse.shutil, "get_terminal_size", return_value=size((60, 20))):
            self.assertFalse(pulse.viewport_below_floor())



FULL_CLEAR = "\033[H\033[2J"


class LiveLoopTests(unittest.TestCase):
    def run_pane(self, keys, clock_step=0.4, sizes=None, args=("--local",), height=36, width=111):
        """Drive run_live with scripted keys and a stepping clock."""
        import itertools

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_path = root / "state.json"
            parsed = pulse.build_parser().parse_args(list(args))
            output = io.StringIO()
            item = recommendation("Alpha")
            size = os.terminal_size
            terminal = mock.patch.object(pulse.shutil, "get_terminal_size", side_effect=sizes) if sizes else nullcontext()
            with mock.patch.object(pulse, "live_input", return_value=nullcontext(4)):
                with mock.patch.object(pulse, "read_live_key", side_effect=keys):
                    with mock.patch.object(pulse, "inspect_workspace", return_value=[]) as workspace:
                        with mock.patch.object(pulse, "inspect_pipelines", return_value=[]):
                            with mock.patch.object(pulse, "fetch_github", return_value=pulse.GithubSnapshot(False)) as github:
                                with mock.patch.object(pulse, "all_recommendations", return_value=[item]):
                                    with mock.patch.object(pulse.time, "monotonic", side_effect=itertools.count(0, clock_step)):
                                        with mock.patch.object(pulse.time, "sleep"):
                                            with mock.patch.object(pulse, "dashboard_width", return_value=width):
                                                with mock.patch.object(pulse, "dashboard_height", return_value=height):
                                                    with mock.patch.object(pulse, "terminal_columns", return_value=width):
                                                        with terminal:
                                                            with redirect_stdout(output):
                                                                self.assertEqual(
                                                                    pulse.run_live(parsed, root, state_path, pulse.Palette(False)), 0
                                                                )
            state = pulse.read_state(state_path)
        return output.getvalue(), workspace.call_count, github.call_count, state

    def test_resize_and_navigation_force_a_full_repaint(self):
        size = os.terminal_size
        rendered, *_rest = self.run_pane(
            [None, "down", "up", KeyboardInterrupt],
            sizes=[size((111, 36)), size((90, 30)), size((90, 30)), size((90, 30)), size((90, 30))],
        )
        self.assertEqual(rendered.count(FULL_CLEAR), 4)  # entry, resize, down, up

    def test_manual_refresh_keeps_existing_full_refresh_behavior(self):
        rendered, workspace, github, _state = self.run_pane(["refresh", None, KeyboardInterrupt], args=())
        self.assertEqual((workspace, github), (2, 2))
        self.assertEqual(rendered.count(FULL_CLEAR), 2)

    def test_under_the_floor_arrows_leave_the_notice_alone(self):
        rendered, workspace, _github, _state = self.run_pane([None, "down", None, "up", KeyboardInterrupt], height=15)
        self.assertEqual(rendered.count(FULL_CLEAR), 1)
        self.assertIn("MAKE THE WINDOW 60x20 OR LARGER", rendered)
        self.assertNotIn("WORKSPACE /", rendered)
        self.assertEqual(workspace, 1)

    def test_interrupt_restores_terminal_and_clears_live_marker(self):
        rendered, *_rest, state = self.run_pane([None, KeyboardInterrupt])
        self.assertTrue(rendered.startswith(pulse.ALT_SCREEN_ENTER + pulse.CURSOR_HIDE))
        self.assertTrue(rendered.endswith(pulse.CURSOR_SHOW + pulse.ALT_SCREEN_LEAVE))
        self.assertNotIn("live", state)
        self.assertIn("listing", state)


class AgentsCommandTests(unittest.TestCase):
    def test_agents_is_a_registered_command_that_never_takes_the_dashboard_pane(self):
        args = pulse.build_parser().parse_args(["agents"])
        self.assertEqual(args.command, "agents")
        self.assertFalse(pulse.wants_live_pane(args))

    def run_agents(self, keys, scans, columns=100, lines=30, sizes=None):
        import itertools

        args = pulse.build_parser().parse_args(["agents"])
        output = io.StringIO()
        output.isatty = lambda: True  # type: ignore[method-assign]  # the live path, not the --once one
        size = os.terminal_size((columns, lines))
        with mock.patch.object(pulse, "live_input", return_value=nullcontext(4)):
            with mock.patch.object(pulse, "read_live_key", side_effect=keys):
                with mock.patch.object(pulse.AgentCollector, "scan", side_effect=scans) as agents:
                    with mock.patch.object(pulse, "inspect_workspace") as workspace:
                        with mock.patch.object(pulse, "fetch_github") as github:
                            with mock.patch.object(pulse, "save_listing") as listing:
                                with mock.patch.object(
                                    pulse.shutil, "get_terminal_size", **({"side_effect": sizes} if sizes else {"return_value": size})
                                ):
                                    with mock.patch.object(pulse.time, "monotonic", side_effect=itertools.count(0, 0.4)):
                                        with redirect_stdout(output):
                                            code = pulse.run_agents(args, Path("/workspace"), pulse.Palette(False))
        self.assertEqual(code, 0)
        workspace.assert_not_called()
        github.assert_not_called()
        listing.assert_not_called()  # the strip never claims numbering ownership
        return output.getvalue(), agents.call_count

    def test_agents_pane_is_the_band_alone_rewritten_in_place(self):
        live = [observation("claude", "a", 0, [event("edit", 1, "pulse")]), observation("codex", "b", 1)]
        rendered, scans = self.run_agents([None, None, None, KeyboardInterrupt], [live] * 10)
        self.assertTrue(rendered.startswith(pulse.ALT_SCREEN_ENTER + pulse.CURSOR_HIDE))
        self.assertTrue(rendered.endswith(pulse.CURSOR_SHOW + pulse.ALT_SCREEN_LEAVE))
        self.assertGreaterEqual(scans, 3)
        self.assertEqual(rendered.count(FULL_CLEAR), 1)  # one clear, then rows rewritten in place
        self.assertIn("// LIVE AGENTS", rendered)
        self.assertIn("╭─────╮", rendered)
        self.assertIn("≡╱◆", rendered)
        self.assertIn("◆╲≡", rendered)  # the frame advanced
        for absent in ("GOOD NEXT MOVES", "WORKSPACE /", "AGENT PIPELINES", "WORTH CONSIDERING"):
            self.assertNotIn(absent, rendered)
        # The dashboard's masthead, labelled for this pane, sits over the band.
        self.assertIn("\033[1;1H\033[2KPROJECT PULSE // AGENTS", rendered)
        self.assertIn("LIVE SIGNAL", rendered)
        self.assertIn("▄", rendered)
        self.assertIn("\033[6;1H\033[2K┌─ // LIVE AGENTS", rendered)
        self.assertIn("\033[13;1H\033[2K", rendered)
        self.assertNotIn("\033[14;1H", rendered)

    def test_agents_pane_reflows_down_to_two_full_cards(self):
        live = [observation("claude", "a", 0)]
        tiny, _scans = self.run_agents([None, KeyboardInterrupt], [live] * 4, columns=19)
        self.assertIn("MAKE THE WINDOW 20…", tiny)  # clipped to the real width, never wrapped
        self.assertNotIn("OR LARGER", tiny)
        short, _scans = self.run_agents([None, KeyboardInterrupt], [live] * 4, lines=7)
        self.assertIn("MAKE THE WINDOW 20x8 OR LARGER", short)
        fits, _scans = self.run_agents([None, KeyboardInterrupt], [live] * 4, columns=20, lines=8)
        self.assertIn("// LIVE AGENTS", fits)
        self.assertIn("01", fits)
        self.assertIn("CLAUDE", fits)
        self.assertIn("╭─────╮", fits)
        self.assertNotIn("PROJECT PULSE", fits)
        self.assertIn("Projects", fits)
        self.assertNotIn("\033[9;1H", fits)
        self.assertEqual(pulse.agent_strip_width(60, 6, False), 10)
        full, _scans = self.run_agents([None, KeyboardInterrupt], [live] * 4, columns=60, lines=8)
        self.assertIn("Projects", full)
        self.assertRegex(ANSI.sub("", full), r"\b\d+(?::\d{2}|h\d{2})\b")
        self.assertNotIn("PROJECT PULSE", full)
        self.assertNotIn("\033[9;1H", full)
        self.assertNotIn("┌─", full)  # the frame yields under 70 columns
        headed, _scans = self.run_agents([None, KeyboardInterrupt], [live] * 4, columns=70, lines=13)
        self.assertIn("PROJECT PULSE // AGENTS", headed)
        self.assertIn("LIVE SIGNAL", headed)
        self.assertIn("╭─────╮", headed)
        self.assertNotIn("\033[14;1H", headed)

    def test_sixty_by_ten_keeps_the_full_six_card_band(self):
        live = [observation("claude", "a", 0, [event("edit", 1, "pulse")]), observation("codex", "b", 1)]
        rendered, _scans = self.run_agents([None, None, None, KeyboardInterrupt], [live] * 10, columns=60, lines=10)
        self.assertEqual(rendered.count(FULL_CLEAR), 1)
        self.assertIn("\033[1;1H\033[2K// LIVE AGENTS", rendered)
        self.assertIn("02 SESSIONS", rendered)
        self.assertIn("CLAUDE", rendered)
        self.assertIn("CODEX", rendered)
        self.assertIn("│ ≡╱◆ │", rendered)
        self.assertIn("│ ◆╲≡ │", rendered)  # the three-row card icon still moves
        self.assertIn("Projects", rendered)
        self.assertRegex(ANSI.sub("", rendered), r"\b\d+(?::\d{2}|h\d{2})\b")
        self.assertIn("pulse", rendered)
        self.assertIn("\033[8;1H", rendered)
        self.assertNotIn("\033[9;1H", rendered)  # rows nine and ten stay empty
        for absent in ("PROJECT PULSE", "PULSE//AGENTS", "still ···"):
            self.assertNotIn(absent, rendered)
        self.assertNotIn("┌─", rendered)
        widest = max(len(ANSI.sub("", line)) for line in rendered.split("\033[2K") for line in [line.split("\033[")[0]])
        self.assertLessEqual(widest, 60)

    def test_resizing_between_forms_repaints_the_whole_screen_once(self):
        live = [observation("claude", "a", 0, [event("edit", 1, "pulse")])]
        sizes = [os.terminal_size((100, 30))] + [os.terminal_size((60, 10))] * 10
        rendered, _scans = self.run_agents([None, None, None, KeyboardInterrupt], [live] * 10, sizes=sizes)
        self.assertEqual(rendered.count(FULL_CLEAR), 2)  # once with the masthead, once without it
        self.assertIn("╭─────╮", rendered)
        self.assertIn("PROJECT PULSE // AGENTS", rendered)
        self.assertIn("// LIVE AGENTS", rendered)
        self.assertNotIn("PULSE//AGENTS", rendered)

    def test_the_agents_masthead_uses_the_dashboard_gradient(self):
        band = pulse.AgentBand()
        band.update([observation("claude", "a", 0)], at(1))
        rows = pulse.agents_frame_rows(band, pulse.Palette(True), 100, 30, 0, datetime.now(timezone.utc))
        wordmark = "".join(rows[1:4])
        self.assertIn("\033[38;2;255;76;176m", wordmark)  # the first cell is pink
        self.assertIn("\033[38;2;139;99;255m", wordmark)  # the last cell is violet
        self.assertNotIn("0;120;190", wordmark)  # the discarded riso blue
        self.assertIn("\033[38;2;250;84;185mLIVE SIGNAL", rows[0])
        dashboard = io.StringIO()
        with redirect_stdout(dashboard):
            pulse.print_masthead(pulse.Palette(True), 100, datetime.now(), live=True)
        self.assertEqual(rows[1:4], dashboard.getvalue().splitlines()[1:4])

    def test_agents_once_prints_one_static_band_to_the_normal_screen(self):
        live = [observation("codex", "c", 0, [event("question", 1, "request_user_input", "q")])]
        output = io.StringIO()
        with mock.patch.object(pulse.AgentCollector, "scan", return_value=live):
            with mock.patch.object(pulse.shutil, "get_terminal_size", return_value=os.terminal_size((100, 30))):
                with redirect_stdout(output):
                    self.assertEqual(pulse.main(["agents", "--once", "--no-color"]), 0)
        rendered = output.getvalue()
        self.assertNotIn(pulse.ALT_SCREEN_ENTER, rendered)
        self.assertEqual(len(rendered.splitlines()), 13)
        self.assertIn("PROJECT PULSE // AGENTS", rendered)
        self.assertIn("STATIC SCAN", rendered)
        self.assertIn("// LIVE AGENTS", rendered)
        self.assertIn(">>", rendered)  # waiting, said by the icon
        self.assertIn("request_use", rendered)  # the tool name, never the question


OSC52 = "\x1b]52;c;aGkK\x07"  # writes the clipboard on terminals that honor OSC 52


class UntrustedInputHardeningTests(unittest.TestCase):
    def test_github_titles_and_repo_names_cannot_reach_the_terminal_raw(self):
        hostile = {
            "number": 7,
            "title": f"Fix login {OSC52}\x1b[2J\rquietly",
            "repository": {"nameWithOwner": f"evil/{OSC52}repo"},
            "updatedAt": "2026-08-01T00:00:00Z",
            "isDraft": True,
        }
        snapshot = pulse.GithubSnapshot(available=True, user="me", open_prs=[hostile], review_requests=[hostile])
        items = pulse.github_recommendations(snapshot, datetime.now(timezone.utc))
        self.assertEqual(len(items), 2)
        for item in items:
            for value in (item.title, item.detail, item.command):
                self.assertNotIn("\x1b", value)
                self.assertNotIn("\x07", value)
                self.assertNotIn("\r", value)
        self.assertIn("Fix login", items[0].detail)  # the readable part survives
        plain, colored = io.StringIO(), io.StringIO()
        with redirect_stdout(plain):
            pulse.print_recommendations(items, pulse.Palette(False))
        self.assertNotIn("\x1b", plain.getvalue())
        with redirect_stdout(colored):
            pulse.print_recommendations(items, pulse.Palette(True))
        # Pulse's own styling is the only escape left on a colored render.
        self.assertIn("\x1b[", colored.getvalue())
        self.assertNotIn("\x1b", ANSI.sub("", colored.getvalue()))

    def test_model_ideas_are_scrubbed_but_prompts_keep_their_paragraphs(self):
        hostile = {
            "kind": "script",
            "title": f"Ship it {OSC52}",
            "detail": "One\x1b[8m hidden detail",
            "prompt": f"Line one {OSC52}\n\nLine three",
        }
        parsed = pulse.parse_ideas({"ideas": [hostile]})[0]
        for name in ("title", "detail", "prompt"):
            self.assertNotIn("\x1b", parsed[name])
            self.assertNotIn("\x07", parsed[name])
        self.assertIn("\n\n", parsed["prompt"])  # pasted as written, paragraphs intact
        self.assertIn("�", parsed["title"])  # the attempt stays visible, not silently dropped

    def test_a_hostile_log_tail_is_neutralized_before_display(self):
        with tempfile.TemporaryDirectory() as temporary:
            log = Path(temporary) / "run.log"
            log.write_text(f"2026-08-30T10:00:00 ok\n2026-08-31T10:00:00 done {OSC52}\x1b[2J\n")
            pipeline = pulse.Pipeline(name="orchestrator")
            pulse.read_pipeline_log(pipeline, log)
            self.assertNotIn("\x1b", pipeline.last_line)
            self.assertIn("done", pipeline.last_line)

    def test_transcript_filenames_and_activity_labels_are_scrubbed(self):
        basename = pulse.sanitized_basename(f"/somewhere/{OSC52}main.py")
        self.assertNotIn("\x1b", basename)
        self.assertTrue(basename.endswith("main.py"))
        ledger = pulse.AgentLedger(at(0))
        ledger.apply(pulse.AgentEvent(at(1), "activity", f"mcp__{OSC52}__tool"))
        self.assertNotIn("\x1b", ledger.activity)
        self.assertIn("tool", ledger.activity)

    def test_state_file_is_owner_only_even_when_it_predates_the_fix(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.json"
            pulse.write_state(path, {"a": 1})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            path.chmod(0o644)  # a state file written before the mode was pinned
            leftover = path.with_suffix(path.suffix + ".tmp")
            leftover.write_text("{}")
            leftover.chmod(0o644)  # and a leftover temp file, likewise
            pulse.write_state(path, {"a": 2})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(path.read_text())["a"], 2)

    def test_a_repo_config_cannot_run_commands_during_the_scan(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo_path = root / "Hostile"
            make_repo(repo_path)
            marker = root / "fsmonitor-ran"
            command("git", "config", "core.fsmonitor", f"touch '{marker}';", cwd=repo_path)
            command("git", "status", cwd=repo_path)
            if not marker.exists():
                self.skipTest("this git does not execute core.fsmonitor commands")
            marker.unlink()
            repo = pulse.inspect_repo(root, repo_path, datetime.now(timezone.utc))
            self.assertFalse(marker.exists())
            self.assertEqual(repo.branch, "main")  # the hardened scan still reads real state
            self.assertTrue(repo.clean)

    def test_idea_pack_names_the_workspace_without_the_absolute_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "Workspace"
            root.mkdir()
            pack = pulse.build_idea_pack(root, [], {}, [], 3)
            self.assertEqual(pack["root"], "Workspace")
            self.assertNotIn(str(root), json.dumps(pack))


if __name__ == "__main__":
    unittest.main()
