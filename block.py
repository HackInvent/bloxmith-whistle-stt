"""Local Whistle STT integration through public block execution and storage APIs."""

import json
from bloxsmith_app.block_api import BlockDefinition, BlockRuntimeOutput, BlockRuntimePreparation, BlockRuntimeResult
from . import logic, ui


# FB1 - Validate local audio and finalized-recording inputs without replaying cached values.
# FB2 - Load pinned, checksummed model assets locally; keep private cache and no API credentials.
# FB3 - Decode bounded clips and emit only validated final transcription/word timestamps.
# FB4 - Terminate conversion/inference on cancellation; retain sources and isolate native state.
# FB5 - Preserve public runtime/package contracts and translated responsive draft settings.
class WhistleSttBlock(BlockDefinition):
    kind = "whistle_stt"

    def ui_assets(self, surface="modal"):
        return list(self.model.get("ui_assets", {}).get(surface, []))

    def prepare_runtime(self, context):
        logic.configuration(context.config)
        return BlockRuntimePreparation()

    def execute_runtime(self, context):
        try:
            config = logic.configuration(context.config)
            if context.input_events:
                names = {int(port.id): port.name for port in context.input_ports}
                values = [event.value for event in context.input_events if names.get(event.input_port_id) == "audio_file"]
                if len(values) != len(context.input_events):
                    raise logic.WhistleError("Unexpected input port.")
            else:
                values = [context.input_value("audio_file", "1")] if context.has_input_value("audio_file", "1") else []
            if not values:
                return BlockRuntimeResult(status="skipped", outputs=[])
            if len(values) != 1:
                raise logic.WhistleError("Send one completed audio clip per activation.")
            resolver = context.services.get("get_block_storage_dir")
            if not callable(resolver):
                raise logic.WhistleError("The block storage service is unavailable.")
            result = logic.transcribe(values[0], config, context.root_dir, resolver(),
                context.services.get("cancel_requested", lambda: False))
            values = {"result": result, "status": {"code": "transcribed" if result["text"].strip() else "no_speech",
                "duration_sec": result["duration_sec"], "elapsed_sec": result["elapsed_sec"]}}
            if result["text"].strip():
                values["text"] = result["text"]
        except (ValueError, TypeError, RuntimeError, OSError) as exc:
            detail = str(exc) if isinstance(exc, logic.WhistleError) else "Local transcription is unavailable; check the input file and prerequisites."
            values = {"status": {"code": "cancelled" if isinstance(exc, logic.Cancelled) else "error", "detail": detail}}
        outputs = [BlockRuntimeOutput(port_id=int(port.id), port_name=port.name,
            value=values[port.name] if port.name == "text" else json.dumps(values[port.name], ensure_ascii=True, allow_nan=False),
            content_type="text/plain" if port.name == "text" else "application/json")
            for port in context.output_ports if port.name in values]
        return BlockRuntimeResult(status="success", outputs=outputs,
            metadata={self.kind: values["status"]}, logs=["[whistle-stt] " + values["status"]["code"]])

    def handle_ui_action(self, *, node, action, values, payload=None):
        if action in {"save_settings", "modal_update_fields", "inspector_update_fields"}:
            patch = (values or {}).get("node_patch") or {}
            if "config" in patch:
                try:
                    logic.configuration({**self.default_config(), **(node.get("config") or {}), **patch["config"]})
                except (ValueError, TypeError) as exc:
                    return {"error": self.translate("block.whistle_stt.error", {"detail": str(exc)}, fallback="Transcription failed: {detail}")}
        return ui.replace_config(super().handle_ui_action(node=node,
            action="modal_update_fields" if action == "save_settings" else action, values=values, payload=payload), node)

    def render_modal(self, *, node, payload=None):
        return ui.modal(self, node, payload)

    def render_inspector_panel(self, *, node, payload=None):
        return ui.inspector(self, node, payload)

    def render_node_card(self, *, node, payload=None):
        config = {**self.default_config(), **(node.get("config") or {})}
        return ui.card(self, node, config["language"])
