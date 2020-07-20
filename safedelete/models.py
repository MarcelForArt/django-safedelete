import warnings

from django.db import models, router
from django.utils import timezone

from .config import (HARD_DELETE, HARD_DELETE_NOCASCADE, NO_DELETE,
                     SOFT_DELETE, SOFT_DELETE_CASCADE)
from .managers import (SafeDeleteAllManager, SafeDeleteDeletedManager,
                       SafeDeleteManager, OrderedSafeDeleteManager, OrderedSafeDeleteDeletedManager,
                       OrderedSafeDeleteAllManager)
from .signals import post_softdelete, post_undelete, pre_softdelete
from .utils import can_hard_delete, related_objects


def is_safedelete_cls(cls):
    for base in cls.__bases__:
        # This used to check if it startswith 'safedelete', but that masks
        # the issue inside of a test. Other clients create models that are
        # outside of the safedelete package.
        if base.__module__.startswith('safedelete.models'):
            return True
        if is_safedelete_cls(base):
            return True
    return False


def is_safedelete(related):
    warnings.warn(
        'is_safedelete is deprecated in favor of is_safedelete_cls',
        DeprecationWarning)
    return is_safedelete_cls(related.__class__)


class SafeDeleteModel(models.Model):
    """Abstract safedelete-ready model.

    .. note::
        To create your safedelete-ready models, you have to make them inherit from this model.

    :attribute deleted:
        DateTimeField set to the moment the object was deleted. Is set to
        ``None`` if the object has not been deleted.

    :attribute _safedelete_policy: define what happens when you delete an object.
        It can be one of ``HARD_DELETE``, ``SOFT_DELETE``, ``SOFT_DELETE_CASCADE``, ``NO_DELETE`` and ``HARD_DELETE_NOCASCADE``.
        Defaults to ``SOFT_DELETE``.

        >>> class MyModel(SafeDeleteModel):
        ...     _safedelete_policy = SOFT_DELETE
        ...     my_field = models.TextField()
        ...
        >>> # Now you have your model (with its ``deleted`` field, and custom manager and delete method)

    :attribute objects:
        The :class:`safedelete.managers.SafeDeleteManager` that returns the non-deleted models.

    :attribute all_objects:
        The :class:`safedelete.managers.SafeDeleteAllManager` that returns the all models (non-deleted and soft-deleted).

    :attribute deleted_objects:
        The :class:`safedelete.managers.SafeDeleteDeletedManager` that returns the soft-deleted models.
    """

    _safedelete_policy = SOFT_DELETE

    deleted = models.DateTimeField(editable=False, null=True)

    objects = SafeDeleteManager()
    all_objects = SafeDeleteAllManager()
    deleted_objects = SafeDeleteDeletedManager()

    class Meta:
        abstract = True

    def save(self, keep_deleted=False, **kwargs):
        """Save an object, un-deleting it if it was deleted.

        Args:
            keep_deleted: Do not undelete the model if soft-deleted. (default: {False})
            kwargs: Passed onto :func:`save`.

        .. note::
            Undeletes soft-deleted models by default.
        """

        # undelete signal has to happen here (and not in undelete)
        # in order to catch the case where a deleted model becomes
        # implicitly undeleted on-save.  If someone manually nulls out
        # deleted, it'll bypass this logic, which I think is fine, because
        # otherwise we'd have to shadow field changes to handle that case.

        was_undeleted = False
        if not keep_deleted:
            if self.deleted and self.pk:
                was_undeleted = True
            self.deleted = None

        super(SafeDeleteModel, self).save(**kwargs)

        if was_undeleted:
            # send undelete signal
            using = kwargs.get('using') or router.db_for_write(self.__class__, instance=self)
            post_undelete.send(sender=self.__class__, instance=self, using=using)

    def undelete(self, force_policy=None, **kwargs):
        """Undelete a soft-deleted model.

        Args:
            force_policy: Force a specific undelete policy. (default: {None})
            kwargs: Passed onto :func:`save`.

        .. note::
            Will raise a :class:`AssertionError` if the model was not soft-deleted.
        """
        current_policy = force_policy or self._safedelete_policy

        assert self.deleted
        self.save(keep_deleted=False, **kwargs)

        if current_policy == SOFT_DELETE_CASCADE:
            for related in related_objects(self):
                if is_safedelete_cls(related.__class__) and related.deleted:
                    related.undelete()

    def delete(self, force_policy=None, **kwargs):
        """Overrides Django's delete behaviour based on the model's delete policy.

        Args:
            force_policy: Force a specific delete policy. (default: {None})
            kwargs: Passed onto :func:`save` if soft deleted.
        """
        current_policy = self._safedelete_policy if (force_policy is None) else force_policy

        if current_policy == NO_DELETE:

            # Don't do anything.
            return

        elif current_policy == SOFT_DELETE:

            # Only soft-delete the object, marking it as deleted.
            self.deleted = timezone.now()
            using = kwargs.get('using') or router.db_for_write(self.__class__, instance=self)
            # send pre_softdelete signal
            pre_softdelete.send(sender=self.__class__, instance=self, using=using)
            self.save(keep_deleted=True, **kwargs)
            # send softdelete signal
            post_softdelete.send(sender=self.__class__, instance=self, using=using)

        elif current_policy == HARD_DELETE:

            # Normally hard-delete the object.
            super(SafeDeleteModel, self).delete()

        elif current_policy == HARD_DELETE_NOCASCADE:

            # Hard-delete the object only if nothing would be deleted with it

            if not can_hard_delete(self):
                self.delete(force_policy=SOFT_DELETE, **kwargs)
            else:
                self.delete(force_policy=HARD_DELETE, **kwargs)

        elif current_policy == SOFT_DELETE_CASCADE:
            # Soft-delete on related objects before
            for related in related_objects(self):
                if is_safedelete_cls(related.__class__) and not related.deleted:
                    related.delete(force_policy=SOFT_DELETE, **kwargs)
            # soft-delete the object
            self.delete(force_policy=SOFT_DELETE, **kwargs)

    @classmethod
    def has_unique_fields(cls):
        """Checks if one of the fields of this model has a unique constraint set (unique=True)

        Args:
            model: Model instance to check
        """
        for field in cls._meta.fields:
            if field._unique:
                return True
        return False

    # We need to overwrite this check to ensure uniqueness is also checked
    # against "deleted" (but still in db) objects.
    # FIXME: Better/cleaner way ?
    def _perform_unique_checks(self, unique_checks):
        errors = {}

        for model_class, unique_check in unique_checks:
            lookup_kwargs = {}
            for field_name in unique_check:
                f = self._meta.get_field(field_name)
                lookup_value = getattr(self, f.attname)
                if lookup_value is None:
                    continue
                if f.primary_key and not self._state.adding:
                    continue
                lookup_kwargs[str(field_name)] = lookup_value
            if len(unique_check) != len(lookup_kwargs):
                continue

            # This is the changed line
            if hasattr(model_class, 'all_objects'):
                qs = model_class.all_objects.filter(**lookup_kwargs)
            else:
                qs = model_class._default_manager.filter(**lookup_kwargs)

            model_class_pk = self._get_pk_val(model_class._meta)
            if not self._state.adding and model_class_pk is not None:
                qs = qs.exclude(pk=model_class_pk)
            if qs.exists():
                if len(unique_check) == 1:
                    key = unique_check[0]
                else:
                    key = models.base.NON_FIELD_ERRORS
                errors.setdefault(key, []).append(
                    self.unique_error_message(model_class, unique_check)
                )
        return errors


class SafeDeleteMixin(SafeDeleteModel):
    """``SafeDeleteModel`` was previously named ``SafeDeleteMixin``.

    .. deprecated:: 0.4.0
        Use :class:`SafeDeleteModel` instead.
    """

    class Meta:
        abstract = True

    def __init__(self, *args, **kwargs):
        warnings.warn('The SafeDeleteMixin class was renamed SafeDeleteModel',
                      DeprecationWarning)
        SafeDeleteModel.__init__(self, *args, **kwargs)



class BaseOrderedSafeDeleteModel(SafeDeleteModel):
    """
    # ADDED BY LEE

    Based on django-ordered-models lib

    A mixin that allows objects to be ordered relative to each other.

    Usage
     - create a model with this mixin
     - add an indexed ``PositiveIntegerField`` to the model
     - set ``order_field_name`` to the name of that field
     - use the same field name in ``Meta.ordering``
    [optional]
     - set ``order_with_respect_to`` to limit order to a subset
     - specify ``order_class_path`` in case of polymorphic classes

    E.g.
        order = models.PositiveIntegerField(_("order"), editable=False, db_index=True)
        order_field_name = "order"

        class Meta:
            abstract = True
            ordering = ("order",)
    """
    objects = OrderedSafeDeleteManager()
    all_objects = OrderedSafeDeleteAllManager()
    deleted_objects = OrderedSafeDeleteDeletedManager()

    order_field_name = None
    order_with_respect_to = None
    order_class_path = None

    def _validate_ordering_reference(self, ref):
        if self.order_with_respect_to is not None:
            self_kwargs = self._meta.default_manager._get_order_with_respect_to_filter_kwargs(
                self
            )
            ref_kwargs = ref._meta.default_manager._get_order_with_respect_to_filter_kwargs(
                ref
            )
            if self_kwargs != ref_kwargs:
                raise ValueError(
                    "{0!r} can only be swapped with instances of {1!r} with equal {2!s} fields.".format(
                        self,
                        self._meta.default_manager.model,
                        " and ".join(["'{}'".format(o) for o in self_kwargs]),
                    )
                )

    def get_ordering_queryset(self, qs=None):
        if qs is None:
            if self.order_class_path:
                model = import_string(self.order_class_path)
                qs = model._meta.default_manager.all()
            else:
                qs = self._meta.default_manager.all()
        return qs.filter_by_order_with_respect_to(self)

    def previous(self):
        """
        Get previous element in this object's ordered stack.
        """
        return self.get_ordering_queryset().below_instance(self).last()

    def next(self):
        """
        Get next element in this object's ordered stack.
        """
        return self.get_ordering_queryset().above_instance(self).first()

    def save(self, *args, **kwargs):
        order_field_name = self.order_field_name
        if getattr(self, order_field_name) is None:
            order = self.get_ordering_queryset().get_next_order()
            setattr(self, order_field_name, order)
        super().save(*args, **kwargs)

    def delete(self, *args, extra_update=None, **kwargs):
        qs = self.get_ordering_queryset()
        extra_update = {} if extra_update is None else extra_update
        qs.above_instance(self).decrease_order(**extra_update)
        super().delete(*args, **kwargs)

    def swap(self, replacement):
        """
        Swap the position of this object with a replacement object.
        """
        self._validate_ordering_reference(replacement)

        order_field_name = self.order_field_name
        order, replacement_order = (
            getattr(self, order_field_name),
            getattr(replacement, order_field_name),
        )
        setattr(self, order_field_name, replacement_order)
        setattr(replacement, order_field_name, order)
        self.save()
        replacement.save()

    def up(self):
        """
        Move this object up one position.
        """
        previous = self.previous()
        if previous:
            self.swap(previous)

    def down(self):
        """
        Move this object down one position.
        """
        _next = self.next()
        if _next:
            self.swap(_next)

    def to(self, order, extra_update=None):
        """
        Move object to a certain position, updating all affected objects to move accordingly up or down.
        """
        if not isinstance(order, int):
            raise TypeError(
                "Order value must be set using an 'int', not using a '{0}'.".format(
                    type(order).__name__
                )
            )

        order_field_name = self.order_field_name
        if order is None or getattr(self, order_field_name) == order:
            # object is already at desired position
            return
        qs = self.get_ordering_queryset()
        extra_update = {} if extra_update is None else extra_update
        if getattr(self, order_field_name) > order:
            qs.below_instance(self).above(order, inclusive=True).increase_order(
                **extra_update
            )
        else:
            qs.above_instance(self).below(order, inclusive=True).decrease_order(
                **extra_update
            )
        setattr(self, order_field_name, order)
        self.save()

    def above(self, ref, extra_update=None):
        """
        Move this object above the referenced object.
        """
        self._validate_ordering_reference(ref)
        order_field_name = self.order_field_name
        if getattr(self, order_field_name) == getattr(ref, order_field_name):
            return
        if getattr(self, order_field_name) > getattr(ref, order_field_name):
            o = getattr(ref, order_field_name)
        else:
            o = self.get_ordering_queryset().below_instance(ref).get_max_order() or 0
        self.to(o, extra_update=extra_update)

    def below(self, ref, extra_update=None):
        """
        Move this object below the referenced object.
        """
        self._validate_ordering_reference(ref)
        order_field_name = self.order_field_name
        if getattr(self, order_field_name) == getattr(ref, order_field_name):
            return
        if getattr(self, order_field_name) > getattr(ref, order_field_name):
            o = self.get_ordering_queryset().above_instance(ref).get_min_order() or 0
        else:
            o = getattr(ref, order_field_name)
        self.to(o, extra_update=extra_update)

    def top(self, extra_update=None):
        """
        Move this object to the top of the ordered stack.
        """
        o = self.get_ordering_queryset().get_min_order()
        self.to(o, extra_update=extra_update)

    def bottom(self, extra_update=None):
        """
        Move this object to the bottom of the ordered stack.
        """
        o = self.get_ordering_queryset().get_max_order()
        self.to(o, extra_update=extra_update)

    class Meta:
        abstract = True
        ordering = ("order",)