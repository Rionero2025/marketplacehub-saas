from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd

from services.db import DATA_DIR, now_iso

BATCH_DIR=DATA_DIR/"batch_memory"


def _clean(value) -> str:
    if pd.isna(value):return ""
    return str(value).strip().removesuffix(".0")


def attach_product_keys(frame: pd.DataFrame) -> pd.DataFrame:
    """Attach stable per-view row keys while preserving the saved snapshot order."""
    result=frame.copy()
    keys=[]
    for index,item in result.iterrows():
        raw=f"{index}\0{_clean(item.get('sku',''))}\0{_clean(item.get('ean',''))}"
        keys.append(hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24])
    result["_batch_key"]=keys
    return result


def _state_path(scope: dict) -> Path:
    identity=json.dumps(scope,ensure_ascii=False,sort_keys=True,separators=(",",":"))
    digest=hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
    return BATCH_DIR/f"{digest}.json"


def load_state(scope: dict) -> dict:
    path=_state_path(scope)
    if path.exists():
        try:
            state=json.loads(path.read_text(encoding="utf-8"))
            if state.get("scope")==scope:return state
        except Exception:pass
    return {"version":1,"scope":scope,"completed":[],"active_batch":None,"history":[],"updated_at":now_iso()}


def save_state(state: dict) -> Path:
    BATCH_DIR.mkdir(parents=True,exist_ok=True)
    path=_state_path(state["scope"]);temp=path.with_suffix(".tmp")
    state["updated_at"]=now_iso()
    temp.write_text(json.dumps(state,ensure_ascii=False,separators=(",",":")),encoding="utf-8")
    temp.replace(path)
    return path


def reset_state(scope: dict) -> None:
    _state_path(scope).unlink(missing_ok=True)


def frame_records(frame: pd.DataFrame) -> list[dict]:
    return [{"key":str(item["_batch_key"]),"sku":_clean(item.get("sku","")),"ean":_clean(item.get("ean",""))}
            for _,item in frame.iterrows()]


def select_next(scope: dict, records: list[dict], requested_count: int) -> tuple[list[str],dict,dict]:
    state=load_state(scope);completed=set(state.get("completed",[]))
    remaining=[item for item in records if item["key"] not in completed]
    chosen=remaining[:max(1,int(requested_count))]
    history=state.get("history",[])
    batch={"number":len(history)+1,"requested_count":int(requested_count),"selected_count":len(chosen),
           "keys":[item["key"] for item in chosen],"first_sku":chosen[0]["sku"] if chosen else "",
           "last_sku":chosen[-1]["sku"] if chosen else "","selected_at":now_iso()}
    state["active_batch"]=batch;save_state(state)
    return batch["keys"],batch,state


def select_range(scope: dict, records: list[dict], start_position: int,
                 end_position: int) -> tuple[list[str],dict,dict]:
    """Select an inclusive 1-based range, skipping products already completed."""
    start=int(start_position);end=int(end_position)
    if not records:
        raise ValueError("Non ci sono prodotti disponibili.")
    if start < 1 or end < 1:
        raise ValueError("Le posizioni devono partire da 1.")
    if start > end:
        raise ValueError("La posizione iniziale non può essere maggiore di quella finale.")
    if end > len(records):
        raise ValueError(f"La posizione finale non può superare {len(records):,}.")

    state=load_state(scope);completed=set(state.get("completed",[]))
    requested=records[start-1:end]
    chosen=[item for item in requested if item["key"] not in completed]
    skipped=len(requested)-len(chosen)
    history=state.get("history",[])
    batch={
        "number":len(history)+1,
        "requested_start":start,
        "requested_end":end,
        "requested_count":len(requested),
        "already_completed_count":skipped,
        "selected_count":len(chosen),
        "keys":[item["key"] for item in chosen],
        "first_sku":chosen[0]["sku"] if chosen else "",
        "last_sku":chosen[-1]["sku"] if chosen else "",
        "selected_at":now_iso(),
    }
    state["active_batch"]=batch;save_state(state)
    return batch["keys"],batch,state


def record_result(scope: dict, selected_records: list[dict], success_keys: set[str] | list[str],
                  failed_keys: set[str] | list[str], status: str, metadata: dict | None = None) -> dict:
    state=load_state(scope);success=set(success_keys);failed=set(failed_keys)
    selected=[item for item in selected_records if item["key"] in success or item["key"] in failed]
    active=state.get("active_batch") or {}
    history=state.get("history",[])
    entry={"number":int(active.get("number") or len(history)+1),
           "requested_count":int(active.get("requested_count") or len(selected)),
           "selected_count":len(selected),"success_count":len(success),"failed_count":len(failed),
           "first_sku":selected[0]["sku"] if selected else "","last_sku":selected[-1]["sku"] if selected else "",
           "status":status,"sent_at":now_iso(),"metadata":metadata or {}}
    for field in ("requested_start","requested_end","already_completed_count"):
        if field in active:entry[field]=active[field]
    history.append(entry);state["history"]=history
    state["completed"]=sorted(set(state.get("completed",[]))|success)
    state["active_batch"]=None;save_state(state)
    return entry


def progress_summary(state: dict, records: list[dict]) -> dict:
    available={item["key"] for item in records};completed=available & set(state.get("completed",[]))
    return {"total":len(available),"completed":len(completed),"remaining":len(available-completed),
            "active":state.get("active_batch"),"history":state.get("history",[])}
