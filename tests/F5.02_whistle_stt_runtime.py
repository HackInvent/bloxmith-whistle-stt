"""FB1/FB3/FB4/FB5: actual framework execution with the real local model in all origins."""

import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT), str(ROOT / "tests"), str(Path(__file__).parent)]
from blocs.whistle_stt.block import WhistleSttBlock
from block_test_packages import install_test_package, surface_payload
from whistle_fixture import install_cache, speech, silence
from ui_smoke_common import (create_project_api, create_run_api, data_edge, graph_payload,
    graph_storage_dir, http_json, isolated_server, text_node, wait_for_run_terminal)


def main():
    block = WhistleSttBlock()
    for mode in ("centralized", "zeromq_active"):
        for origin in (None, "managed", "linked"):
            with isolated_server() as server:
                audio = speech(server.root_dir)
                node = block.build_node_payload(node_id="whistle", position={"x": 440, "y": 160},
                    config_overrides={"allowed_root": str(server.root_dir), "download_missing": False, "language": "en"})
                if origin:
                    model = install_test_package(server, "whistle_stt", origin=origin)
                    node["block_version"] = model["version"]
                    for surface in ("modal", "inspector_panel", "node_card"):
                        rendered = surface_payload(server, model, node, surface)["html"]
                        assert "{{" not in rendered
                        if surface != "node_card":
                            assert 'data-block-config-field="download_missing"' in rendered
                document = graph_payload("Whistle real model", [text_node("source", "Completed clip", str(audio), 70, 160), node],
                    [data_edge("audio-file", "source", 1, "whistle", 1)])
                project = create_project_api(server, document=document)["project"]
                storage = graph_storage_dir(server, project) / "instances" / "1" / "blocs" / "whistle"
                install_cache(storage)
                result = create_run_api(server, document, project_id=project["graph_id"], runtime_mode=mode)
                result = wait_for_run_terminal(server, result["run_id"], timeout_sec=30)
                assert result["status"] == "success", result.get("logs")
                values = result["output_values"]
                assert "weather" in values["whistle:1"]["value"].lower(), values
                parsed = json.loads(values["whistle:2"]["value"])
                assert parsed["final"] and parsed["words"] and parsed["model_version"] == "2.0.0"
                assert json.loads(values["whistle:3"]["value"])["code"] == "transcribed"
                assert not list(storage.glob("whistle-job-*"))
                # A new Run must reuse persistent assets without downloads or leaked native state.
                silent = server.root_dir / "quiet.wav"; silence(silent, 1)
                document["nodes"][0]["outputs"][0]["text"] = str(silent)
                (graph_storage_dir(server, project) / "graph.json").write_text(
                    json.dumps({**document, "graph_id": project["graph_id"]}), encoding="utf-8")
                http_json(server.base_url, f"/api/projects/{project['graph_id']}/graph/reload", method="POST", payload={})
                second = create_run_api(server, document, project_id=project["graph_id"], runtime_mode=mode)
                second = wait_for_run_terminal(server, second["run_id"], timeout_sec=30)
                assert second["status"] == "success", second.get("logs")
                assert not second["output_values"].get("whistle:1", {}).get("value")
                assert json.loads(second["output_values"]["whistle:3"]["value"])["code"] == "no_speech"
            print("[ok] Whistle real CPU runtime " + mode + " " + str(origin), flush=True)


if __name__ == "__main__":
    main()
