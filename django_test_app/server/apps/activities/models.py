from typing import final

from django.db import models


@final
class Activity(models.Model):
    """
    Marketing activity whose whole configuration is stored as JSON.

    The config document has two independent sections: ticket settings
    (prices) and registration settings (sign-up switch and the capacity
    limit). Operations staff edit such documents through a partial-save
    endpoint, so the JSON value must never be replaced wholesale.
    """

    name = models.CharField(max_length=255)
    #: Sparse JSON document, e.g.
    #: ``{"tickets": {"price": 99.0},
    #:    "registration": {"enabled": True, "capacity": 100}}``.
    config = models.JSONField(default=dict)
    updated_at = models.DateTimeField(auto_now=True)
