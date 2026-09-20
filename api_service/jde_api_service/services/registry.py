"""
Wiring for this service's own services. Kept in one small module so
routers depend on functions here rather than constructing services
themselves -- this is the "service modules underneath the API, not
business logic in route handlers" boundary in practice.

Deliberately NOT cached as module-level singletons: each of these is
just a thin wrapper around a directory path (construction is cheap),
and building fresh per call means tests can point settings.data_dir at
a temp directory without fighting stale global state.
"""

from __future__ import annotations

import os

from ..config import settings
from .architecture_review_service import ArchitectureReviewService
from .business_domain_service import BusinessDomainService
from .change_request_service import ChangeRequestService
from .change_service import ChangeService
from .customer_link_service import CustomerLinkService
from .delivery_queue_service import DeliveryQueueService
from .domain_review_service import DomainReviewService
from .enhancement_run_service import EnhancementRunService
from .metrics_service import MetricsService


def get_change_request_service() -> ChangeRequestService:
    return ChangeRequestService(os.path.join(settings.data_dir, "change_requests"))


def get_customer_link_service() -> CustomerLinkService:
    return CustomerLinkService(os.path.join(settings.data_dir, "customer_links"))


def get_enhancement_run_service() -> EnhancementRunService:
    return EnhancementRunService(os.path.join(settings.data_dir, "enhancement_runs"))


def get_business_domain_service() -> BusinessDomainService:
    return BusinessDomainService(os.path.join(settings.data_dir, "business_domains"))


def get_domain_review_service() -> DomainReviewService:
    return DomainReviewService(os.path.join(settings.data_dir, "domain_reviews"))


def get_delivery_queue_service() -> DeliveryQueueService:
    return DeliveryQueueService(os.path.join(settings.data_dir, "delivery_queue"))


def get_architecture_review_service() -> ArchitectureReviewService:
    return ArchitectureReviewService(os.path.join(settings.data_dir, "architecture_reviews"))


def get_change_service() -> ChangeService:
    return ChangeService(
        get_change_request_service(),
        get_customer_link_service(),
        get_enhancement_run_service(),
        get_domain_review_service(),
        get_architecture_review_service(),
    )


def get_metrics_service() -> MetricsService:
    return MetricsService(get_change_service(), get_business_domain_service(), get_delivery_queue_service())
