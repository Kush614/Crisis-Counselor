"""Thin Cekura REST client (stdlib urllib — no dependency).

Endpoints (from docs.cekura.ai, base https://api.cekura.ai, auth header
X-CEKURA-API-KEY):

  POST /test_framework/v1/scenarios/run_scenarios_pipecat_v2/
       body {"scenarios":[{"scenario":<id>}], "frequency":1}  -> run object
  GET  /test_framework/v1/runs/bulk/?run_ids=1,2,3            -> [run, ...]
  GET  /test_framework/v1/results/{id}/                       -> aggregate result

The exact shape of the run-trigger response isn't fully documented, so
``extract_run_ids`` is intentionally defensive and logs the raw payload — tune it
against a real response at the event if needed.
"""

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

TERMINAL_OK = {"completed", "evaluating", "success"}
TERMINAL_BAD = {"failed", "timeout", "cancelled"}


class CekuraError(RuntimeError):
    pass


class CekuraClient:
    def __init__(self, api_key: str, base_url: str = "https://api.cekura.ai") -> None:
        if not api_key:
            raise CekuraError("CEKURA_API_KEY is required for live runs")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")

    def _request(
        self, method: str, path: str, body: dict | None = None, params: dict | None = None
    ) -> Any:
        url = f"{self.base_url}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("X-CEKURA-API-KEY", self.api_key)
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read().decode()
        except urllib.error.HTTPError as e:
            raise CekuraError(f"{method} {path} -> {e.code}: {e.read().decode()[:500]}") from e
        except urllib.error.URLError as e:
            raise CekuraError(f"{method} {path} failed: {e}") from e
        return json.loads(raw) if raw else {}

    # --- API surface -------------------------------------------------------

    def run_scenarios_pipecat(self, scenario_ids: list[int], frequency: int = 1) -> dict:
        """Trigger the configured scenarios against the connected Pipecat agent."""
        body = {
            "scenarios": [{"scenario": sid} for sid in scenario_ids],
            "frequency": frequency,
        }
        # The -external variant is the API-key surface the official SDK/CLI use.
        return self._request(
            "POST", "/test_framework/v1/scenarios-external/run_scenarios_pipecat_v2/", body=body
        )

    def get_runs_bulk(self, run_ids: list[int]) -> list[dict]:
        """Fetch run objects (status, metrics, transcript) by ID."""
        params = {"run_ids": ",".join(str(r) for r in run_ids)}
        resp = self._request("GET", "/test_framework/v1/runs/bulk/", params=params)
        return resp if isinstance(resp, list) else resp.get("runs", [])

    def get_result(self, result_id: int) -> dict:
        """Fetch the aggregate result (success_rate, worst_performing_metrics, ...)."""
        return self._request("GET", f"/test_framework/v1/results/{result_id}/")


def extract_run_ids(run_response: dict) -> tuple[list[int], int | None]:
    """Pull run IDs (and an optional aggregate result id) from a trigger response.

    Tries the shapes seen across the docs; returns ([], None) if none found so the
    caller can log the raw payload and the operator can adjust.
    """
    result_id = None
    for key in ("result", "result_id", "id"):
        v = run_response.get(key)
        if isinstance(v, int):
            result_id = v
            break
        if isinstance(v, dict) and isinstance(v.get("id"), int):
            result_id = v["id"]
            break

    run_ids: list[int] = []
    runs = run_response.get("runs")
    if isinstance(runs, list):
        for item in runs:
            if isinstance(item, int):
                run_ids.append(item)
            elif isinstance(item, dict) and isinstance(item.get("id"), int):
                run_ids.append(item["id"])
    elif isinstance(runs, dict):  # results-style: {"<id>": {...}}
        run_ids = [int(k) for k in runs.keys() if str(k).isdigit()]

    if not run_ids:
        for key in ("run_ids", "ids"):
            v = run_response.get(key)
            if isinstance(v, list):
                run_ids = [int(x) for x in v]
                break

    return run_ids, result_id
