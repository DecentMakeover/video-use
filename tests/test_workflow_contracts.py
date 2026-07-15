from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "SKILL.md"
ARCHITECTURE = ROOT / "docs" / "architecture.md"
RUN_EXAMPLE = ROOT / "workflows" / "run.example.json"
WORKFLOWS = {
    "street-interview": ROOT / "workflows" / "street-interview.md",
    "podcast-clips": ROOT / "workflows" / "podcast-clips.md",
    "podcast-edit": ROOT / "workflows" / "podcast-edit.md",
}


class WorkflowContractTests(unittest.TestCase):
    def test_router_references_every_workflow(self) -> None:
        skill = SKILL.read_text(encoding="utf-8")
        for workflow_id, path in WORKFLOWS.items():
            self.assertTrue(path.is_file(), workflow_id)
            self.assertIn(f"workflows/{workflow_id}.md", skill)

    def test_each_workflow_declares_scope_approvals_and_completion(self) -> None:
        for workflow_id, path in WORKFLOWS.items():
            text = path.read_text(encoding="utf-8")
            self.assertIn("**Maturity:**", text, workflow_id)
            self.assertIn("## Lifecycle and approvals", text, workflow_id)
            self.assertIn("## Complete when", text, workflow_id)
            self.assertIn("## Current limits", text, workflow_id)

    def test_run_example_has_applicable_approval_semantics(self) -> None:
        state = json.loads(RUN_EXAMPLE.read_text(encoding="utf-8"))
        self.assertEqual(state["version"], 1)
        self.assertEqual(state["workflow"], "generic")
        self.assertIn(state["status"], {"active", "waiting_for_user", "complete", "blocked"})
        self.assertIn(state["phase"], {"inventory", "strategy", "selection", "preview", "qc", "final"})
        for approval in state["approvals"].values():
            self.assertIn(approval["status"], {"pending", "approved", "rejected", "not_required"})
            if approval["status"] == "not_required":
                self.assertFalse(approval["required"])

    def test_architecture_covers_agent_native_contracts(self) -> None:
        text = ARCHITECTURE.read_text(encoding="utf-8")
        for phrase in (
            "Capability parity",
            "Agent-native checklist",
            "Project state",
            "Approval gates",
            "Completion and resume",
            "Context limits",
            "Deferred capabilities",
        ):
            self.assertIn(phrase, text)

    def test_skill_uses_edl_parent_relative_paths(self) -> None:
        skill = SKILL.read_text(encoding="utf-8")
        self.assertNotIn('"file": "edit/animations/', skill)
        self.assertNotIn('"subtitles": "edit/master.srt"', skill)
        self.assertIn('"file": "animations/', skill)
        self.assertIn('"subtitles": "master.srt"', skill)

    def test_skill_has_external_upload_gate_and_explicit_run_state(self) -> None:
        skill = SKILL.read_text(encoding="utf-8")
        self.assertIn("explicit approval before uploading", skill)
        self.assertIn("edit/run.json", skill)

    def test_current_multi_output_and_cache_limits_are_explicit(self) -> None:
        clips = WORKFLOWS["podcast-clips"].read_text(encoding="utf-8")
        architecture = ARCHITECTURE.read_text(encoding="utf-8")
        self.assertIn("one approved clip per run", clips)
        self.assertIn("filename stem", architecture)

    def test_fork_install_urls_do_not_point_back_to_upstream(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        install = (ROOT / "install.md").read_text(encoding="utf-8")
        clone = "git clone https://github.com/DecentMakeover/video-use"
        self.assertIn(clone, readme)
        self.assertIn(clone, install)


if __name__ == "__main__":
    unittest.main()
