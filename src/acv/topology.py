"""Service call graph and the expected configuration in each region."""

from dataclasses import dataclass
from types import MappingProxyType
from typing import Tuple

from .models import Target


# Edges point from the caller to its downstream dependency.
DEPENDENCIES = MappingProxyType({
    "frontend": ("gateway", "recommendation"),
    "gateway": ("auth", "cart", "checkout", "pricing"),
    "cart": ("pricing", "inventory"),
    "checkout": ("payment", "shipping", "inventory", "pricing"),
    "payment": ("currency", "ledger"),
    "currency": (),
    "shipping": ("inventory",),
    "inventory": (),
    "pricing": ("currency",),
    "auth": (),
    "recommendation": ("inventory", "pricing"),
    "ledger": (),
})
SERVICES = tuple(DEPENDENCIES)
REGIONS = ("us-east", "us-west", "eu-central", "ap-south", "ap-east", "latam")
FLAGS = MappingProxyType(dict(zip(SERVICES, (
    "new_storefront", "route_v2", "cart_quotes", "checkout_v2", "payment_retry",
    "fx_rounding", "carrier_v2", "reserve_stock", "regional_price", "token_v2",
    "personalized", "ledger_batch",
))))
_CURRENCIES = ("USD", "USD", "EUR", "INR", "JPY", "BRL")
_TIMEOUTS_MS = (220, 240, 270, 310, 330, 360)
REGION_WEIGHTS = MappingProxyType(dict(zip(REGIONS, (.29, .23, .20, .12, .10, .06))))


@dataclass(frozen=True)
class ConfigOverlay:
    currency: str
    flag_enabled: bool
    timeout_ms: int


def target_for(service: str, region: str) -> Target:
    return Target(service, region, FLAGS[service])


TARGETS = tuple(target_for(service, region) for service in SERVICES for region in REGIONS)
CONFIG_OVERLAYS = MappingProxyType({
    target: ConfigOverlay(
        _CURRENCIES[region_index],
        (service_index + 2 * region_index) % 5 < 3,
        _TIMEOUTS_MS[region_index] + 5 * (service_index % 4),
    )
    for service_index, service in enumerate(SERVICES)
    for region_index, region in enumerate(REGIONS)
    for target in (target_for(service, region),)
})


def impact_services(changed_service: str) -> Tuple[str, ...]:
    """List the changed service and its transitive callers in breadth-first order."""
    if changed_service not in DEPENDENCIES:
        raise ValueError(f"unknown service: {changed_service}")
    ordered = [changed_service]
    seen = {changed_service}
    for dependency in ordered:
        for caller, downstream in DEPENDENCIES.items():
            if dependency in downstream and caller not in seen:
                seen.add(caller)
                ordered.append(caller)
    return tuple(ordered)
