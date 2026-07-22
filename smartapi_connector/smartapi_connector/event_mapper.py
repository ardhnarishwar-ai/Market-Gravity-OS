"""event_mapper

Provider-agnostic event mapper that converts a raw provider response (dict)
into an immutable Universal Event Model object.

Constraints observed:
- Preserves the raw payload unchanged.
- Populates envelope only from available provider data; does not invent fields.
- Applies classification labels only if provided by an external PROJECT_MASTER mapping
  or an explicit mapping argument.
- Adds fields: schema_version, producer, timestamps, metadata, provenance,
  validation, and dependencies in the event envelope.
- Produces immutable Event objects (dataclass(frozen=True)).
- No business logic, no derived calculations, no provider-specific assumptions.

Usage:
  event = map_raw_to_event(raw_payload, provider="smartapi", event_type="quote")

If a PROJECT_MASTER module is present and defines CLASSIFICATION_MAP, it will be used
for classification labels. Otherwise, no classification labels are applied.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
import copy

# Attempt to import classification mapping from PROJECT_MASTER if available.
try:
    from PROJECT_MASTER import CLASSIFICATION_MAP  # type: ignore
except Exception:
    CLASSIFICATION_MAP = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class EventEnvelope:
    schema_version: str
    producer: Optional[str]
    timestamps: Dict[str, Optional[str]]
    metadata: Dict[str, Any]
    provenance: Dict[str, Any]
    validation: Dict[str, Any]
    dependencies: List[Dict[str, Any]]
    classification: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Event:
    envelope: EventEnvelope
    raw: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {"envelope": asdict(self.envelope), "raw": copy.deepcopy(self.raw)}


def _apply_classification(provider: str, event_type: Optional[str], mapping: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Return classification labels from mapping for the provider/event_type.

    If mapping is None, return empty dict. Mapping is expected to be a dict keyed by
    provider or (provider, event_type) tuples. This function does NOT invent labels.
    """
    if not mapping:
        return {}

    # mapping may contain keys like 'provider' or 'provider:event_type'
    labels = {}

    # exact key with provider and event_type
    if event_type:
        key = f"{provider}:{event_type}"
        if key in mapping:
            labels.update(mapping[key])

    # fallback to provider-level mapping
    if provider in mapping:
        labels.update(mapping[provider])

    return labels


def map_raw_to_event(
    raw_payload: Dict[str, Any],
    provider: str,
    event_type: Optional[str] = None,
    schema_version: str = "1.0",
    producer: Optional[str] = None,
    classification_mapping: Optional[Dict[str, Any]] = None,
) -> Event:
    """Map a raw provider payload to an immutable Event.

    - raw_payload: the provider response object (kept intact in Event.raw)
    - provider: logical provider name (e.g., "smartapi")
    - event_type: optional provider-declared event type
    - schema_version: version identifier for the envelope schema
    - producer: optional producer identifier (if unknown, pass None)
    - classification_mapping: optional mapping used to apply classification labels.
      If omitted, PROJECT_MASTER.CLASSIFICATION_MAP will be used when available.

    The function extracts only fields that are present in raw_payload; it will not
    invent or compute missing fields. Timestamps are populated conservatively using
    provider-provided timestamps when present and mapping time for mapped_at.
    """
    # Preserve raw payload exactly (work on a shallow copy for safety)
    raw_copy = copy.deepcopy(raw_payload)

    # Timestamps: prefer provider-provided fields if present, otherwise leave None.
    # Common provider keys that may exist: 'timestamp', 'ts', 'created_at', 'received_at'
    provider_ts = None
    for k in ("timestamp", "ts", "created_at", "received_at", "time"):
        if isinstance(raw_payload.get(k), str):
            provider_ts = raw_payload.get(k)
            break

    timestamps: Dict[str, Optional[str]] = {
        "provider_timestamp": provider_ts,
        "mapped_at": _now_iso(),
    }

    # Metadata: include provider, event_type when available, and any provider-declared ids
    metadata: Dict[str, Any] = {
        "provider": provider,
    }
    if event_type:
        metadata["event_type"] = event_type

    # If provider supplies an id field, include it where present but do not invent one
    for id_key in ("id", "event_id", "uuid", "trace_id"):
        if id_key in raw_payload:
            metadata["provider_id"] = raw_payload[id_key]
            break

    # Provenance: record source minimal info
    provenance: Dict[str, Any] = {
        "source": provider,
        "ingest_time": _now_iso(),
    }
    # If provider includes transport info, copy it
    for pkey in ("source_host", "source_ip", "feed", "channel"):
        if pkey in raw_payload:
            provenance[pkey] = raw_payload[pkey]

    # Validation: no business validation performed; include a placeholder indicating
    # that no schema validation was executed here. Do not assert validity.
    validation: Dict[str, Any] = {
        "schema_checked": False,
        "valid": None,
        "messages": [],
    }

    # Dependencies: empty by default; mappers can populate if provider exposes linked ids
    dependencies: List[Dict[str, Any]] = []
    # If provider exposes related_ids, copy them
    if isinstance(raw_payload.get("related_ids"), list):
        for rid in raw_payload.get("related_ids", []):
            dependencies.append({"id": rid})

    # Classification: use provided mapping or PROJECT_MASTER if available
    mapping = classification_mapping if classification_mapping is not None else CLASSIFICATION_MAP
    classification = _apply_classification(provider, event_type, mapping)

    envelope = EventEnvelope(
        schema_version=schema_version,
        producer=producer,
        timestamps=timestamps,
        metadata=metadata,
        provenance=provenance,
        validation=validation,
        dependencies=dependencies,
        classification=classification,
    )

    return Event(envelope=envelope, raw=raw_copy)
