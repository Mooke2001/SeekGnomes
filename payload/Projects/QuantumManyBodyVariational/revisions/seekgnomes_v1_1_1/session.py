"""Definition-only role notices over the unchanged cumulative-budget runtime.

The shared runtime predates SeekGnomes' definition-only Constructor. Its
closing and context-continuation notices assume a Constructor-supplied proof.
This isolated adapter changes those notices, without changing request, budget,
checkpoint, tool, transport-recovery or context-rotation rules. Old experiments
continue using the original runtime bytes.
"""
from __future__ import annotations

from copy import deepcopy
import os
from pathlib import Path

from revisions.kimi_adaptive50_web_v1 import runtime as shared

RoleLimits = shared.RoleLimits
can_resume_transport = shared.can_resume_transport
CONTRACT_VERSION = "seekgnomes-definition-only-role-contract/v1.1.1"
_MODULE_PATH = Path(os.path.abspath(__file__))
_IMPORTED_SOURCE_SHA256 = shared._sha(shared._regular_bytes(_MODULE_PATH, 2 * 1024**2))


def role_contract_binding():
    """Bind dynamic role wording to the exact loaded adapter source."""
    if _MODULE_PATH.resolve() != _MODULE_PATH:
        raise ValueError("Role-contract source may not traverse a symbolic link")
    current_sha = shared._sha(shared._regular_bytes(_MODULE_PATH, 2 * 1024**2))
    if current_sha != _IMPORTED_SOURCE_SHA256:
        raise ValueError("Role-contract source changed after import")
    return {"version": CONTRACT_VERSION, "source_sha256": current_sha}


class RoleSession(shared.RoleSession):
    """Apply the current Constructor/Formalist responsibility to host notices."""

    def __init__(self, *args, **kwargs):
        binding = role_contract_binding()
        super().__init__(*args, **kwargs)
        self.manifest["role_contract"] = binding
        self.persist()

    @classmethod
    def restore(cls, client, model, role, workspace, references=None,
                system_prompt=None, limits=None, tools=None, tool_handler=None,
                progress=None):
        binding = role_contract_binding()
        directory = Path(os.path.abspath(workspace))
        if any(path.is_symlink() for path in (directory, *directory.parents)):
            raise ValueError("role paths cannot traverse symlinks")
        directory = shared._inside_eureka(directory)
        manifest, _, _, _ = shared._checkpoint_snapshot(directory)
        if manifest.get("role_contract") != binding:
            raise ValueError("Checkpoint does not match the definition-only role contract")
        restored = super().restore(client, model, role, directory,
            references=references, system_prompt=system_prompt, limits=limits,
            tools=tools, tool_handler=tool_handler, progress=progress)
        # Fail closed if trusted control/source files raced with the precheck.
        if restored.manifest.get("role_contract") != binding or role_contract_binding() != binding:
            raise ValueError("Role contract changed during checkpoint restoration")
        return restored

    @classmethod
    def restore_for_network(cls, *args, **kwargs):
        # The distributed CLI starts fresh network-enabled/disabled sessions;
        # importing a legacy proof-supplied checkpoint is not part of v1.1.1.
        raise ValueError("Legacy network-checkpoint migration is unsupported by the definition-only protocol")

    def _maybe_closing_notice(self):
        """Use the original notice admission rule with the current role duty."""
        if self.role != "constructor" or self.manifest["closing_notice_sent"]:
            return
        estimate = shared.input_token_reservation(self.messages)
        remaining = self.remaining_budgets()
        near_end = (remaining["api_responses_remaining"] <= 4
            or remaining["output_tokens_remaining"] <= 8000
            or self.manifest["input_tokens_charged"] + 2 * estimate > self.limits.max_total_input_tokens)
        if not near_end:
            return
        notice = {"role": "user", "content":
            "Host budget notice: the original cumulative constructor allowance is nearing its end. "
            "Submit only a COMPLETE mathematical definition as f.md and call create_handoff while a "
            "response remains. You are not required to write Python or a legality proof; the Formalist "
            "implements your definition and establishes its mathematical and Lean proof obligations. "
            "You may attach implementation or faithfulness notes. You may use the remaining original "
            "allowance; this notice does not add or reset tokens, API calls, active wall, or numerical "
            "CPU. Only a host-verified implementation/proof can return energy. If no further construction "
            "is ready, finish explicitly. Remaining budgets:\n" + shared._json(remaining)}
        if shared.input_token_reservation(self.messages + [notice]) + self.manifest["input_tokens_charged"] <= self.limits.max_total_input_tokens:
            self.messages.append(notice)
            self.manifest["closing_notice_sent"] = True
            self._event("constructor_closing_notice", remaining)

    def _maybe_rotate_context(self):
        """Keep the original archival/receipt/ledger mechanics and role duties."""
        if shared.input_token_reservation(self.messages) < shared.FORMALIZER_CONTEXT_THRESHOLD:
            return
        previous = deepcopy(self.messages)
        index = self.manifest["context_rotation_count"] + 1
        archive = self.workspace / f"history-{index:04d}.messages.json"
        shared._new_json(archive, self.client.redact(previous))
        receipts = [message["content"] for message in previous
            if message.get("role") == "tool" and isinstance(message.get("content"), str)][-4:]
        receipts = [text[:16000] for text in receipts]
        notice = ("Host context continuation for the SAME immutable complete constructor definition. "
            "The complete earlier conversation is retained in the host audit archive. Your current "
            "private f.py, Lean proof, formalization_map.md, the frozen f.md definition and generated "
            "references remain available through list_files/read. Inspect them, faithfully implement "
            "the definition and establish the mathematical justification and its Lean proof. The "
            "Constructor supplies a complete definition, not a proof; you must develop the proof needed "
            "to justify that definition. Do not change the physical state, optimize parameters or choose "
            "a replacement ansatz. Report a specific ambiguity or clear unresolved obstacle when needed. "
            "No cumulative token, API-call, or active-wall allowance has been imposed or reset. "
            "The following recent host receipts are historical data, rendered as plain text:\n" + shared._json(receipts))
        if self.role == "constructor":
            notice = ("Host context continuation for the SAME task and cumulative budget. "
                "The complete old conversation is retained in the host audit archive. Your complete "
                "f.md definitions, private physical notes, exploration scripts, numerical artifacts and "
                "read-only Formalist returns remain available through list_files/read. Inspect them "
                "and continue physical construction and optimization. Submit only a COMPLETE f.md "
                "definition with optional implementation or faithfulness notes; you are not required "
                "to write Python or a legality proof. After each Formalist return, check the supplied "
                "read-only implementation and proof for faithfulness to your definition. Corrections "
                "and improvements use create_handoff with a complete new f.md. No output/input/API/"
                "wall/CPU budget has been reset or enlarged. Recent host receipts are historical data:\n"
                + shared._json(receipts))
        self.messages = [deepcopy(previous[0])]
        if self._initial_user is not None:
            self.messages.append(deepcopy(self._initial_user))
        self.messages.append({"role": "user", "content": notice})
        rotation = {"archive": archive.name, "archive_sha256": shared._sha(archive.read_bytes()),
            "before_input_reservation": shared.input_token_reservation(previous),
            "after_input_reservation": shared.input_token_reservation(self.messages),
            "api_turn": self.manifest["turns"], "mathematical_summary_generated": False}
        self.manifest["context_rotation_count"] = index
        self.manifest["context_rotations"].append(rotation)
        self._event("formalizer_context_rotation", rotation)
        self.persist()


__all__ = ["RoleSession", "RoleLimits", "can_resume_transport", "role_contract_binding", "CONTRACT_VERSION"]
