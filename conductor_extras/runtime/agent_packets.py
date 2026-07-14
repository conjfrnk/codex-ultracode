import json
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from .errors import ValidationError


MAX_AGENT_PACKETS = 10000
MAX_AGENT_ITEMS_PER_PACKET = 64
BALANCED_AGENT_MAP_MAX_PACKETS = 25


@dataclass(frozen=True)
class AgentPacket:
    items: Tuple[str, ...]

    @property
    def value(self) -> str:
        if len(self.items) == 1:
            return self.items[0]
        return json.dumps(list(self.items), ensure_ascii=True, separators=(",", ":"))

    @property
    def label(self) -> str:
        if len(self.items) == 1:
            return self.items[0]
        return "%s (+%d more)" % (self.items[0], len(self.items) - 1)


def packetize_agent_items(items: Sequence[str], max_packets: Optional[int] = None) -> List[AgentPacket]:
    if not isinstance(items, (list, tuple)) or not items:
        raise ValidationError("agent_map packetization requires a non-empty item sequence")
    if not all(isinstance(item, str) and item for item in items):
        raise ValidationError("agent_map packetization requires non-empty string items")
    if max_packets is None:
        return [AgentPacket((item,)) for item in items]
    if (
        not isinstance(max_packets, int)
        or isinstance(max_packets, bool)
        or max_packets < 1
        or max_packets > MAX_AGENT_PACKETS
    ):
        raise ValidationError("agent_map max_packets must be an integer from 1 to %d" % MAX_AGENT_PACKETS)
    if len(items) <= max_packets:
        return [AgentPacket((item,)) for item in items]
    items_per_packet = (len(items) + max_packets - 1) // max_packets
    if items_per_packet > MAX_AGENT_ITEMS_PER_PACKET:
        minimum = (len(items) + MAX_AGENT_ITEMS_PER_PACKET - 1) // MAX_AGENT_ITEMS_PER_PACKET
        raise ValidationError(
            "agent_map max_packets %d cannot safely pack %d items; use at least %d packets"
            % (max_packets, len(items), minimum)
        )
    return [
        AgentPacket(tuple(items[offset : offset + items_per_packet]))
        for offset in range(0, len(items), items_per_packet)
    ]
