from typing import final

from django.db import models


@final
class ShelfStatus(models.TextChoices):
    """Possible product shelf statuses used by the operations backend."""

    #: Product is listed, i.e. ``上架``.
    ON_SHELF = 'on', 'On shelf'
    #: Product is delisted, i.e. ``下架``.
    OFF_SHELF = 'off', 'Off shelf'


@final
class Product(models.Model):
    """Goods that operations staff put on and take off the shelf."""

    sku = models.CharField(max_length=64, unique=True)
    name = models.CharField(max_length=255)
    status = models.CharField(
        max_length=8,
        choices=ShelfStatus.choices,
        default=ShelfStatus.OFF_SHELF,
        db_index=True,
    )
    updated_at = models.DateTimeField(auto_now=True)


@final
class ShelfOperationLog(models.Model):
    """Audit record that must be written together with a status change."""

    product = models.ForeignKey(
        Product,
        on_delete=models.CASCADE,
        related_name='shelf_logs',
    )
    action = models.CharField(max_length=32)
    created_at = models.DateTimeField(auto_now_add=True)
