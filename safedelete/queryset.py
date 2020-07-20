from distutils.version import LooseVersion
from functools import partial, reduce

import django
from django.db.models import query
from django.db.models.query_utils import Q
from django.db.models.constants import LOOKUP_SEP
from django.db.models import Max, Min, F
from django.utils.module_loading import import_string

from .config import (DELETED_INVISIBLE, DELETED_ONLY_VISIBLE, DELETED_VISIBLE,
                     DELETED_VISIBLE_BY_FIELD)


class SafeDeleteQueryset(query.QuerySet):
    """Default queryset for the SafeDeleteManager.

    Takes care of "lazily evaluating" safedelete QuerySets. QuerySets passed
    within the ``SafeDeleteQueryset`` will have all of the models available.
    The deleted policy is evaluated at the very end of the chain when the
    QuerySet itself is evaluated.
    """
    _safedelete_filter_applied = False

    def delete(self, force_policy=None):
        """Overrides bulk delete behaviour.

        .. note::
            The current implementation loses performance on bulk deletes in order
            to safely delete objects according to the deletion policies set.

        .. seealso::
            :py:func:`safedelete.models.SafeDeleteModel.delete`
        """
        assert self.query.can_filter(), "Cannot use 'limit' or 'offset' with delete."
        # TODO: Replace this by bulk update if we can
        for obj in self.all():
            obj.delete(force_policy=force_policy)
        self._result_cache = None
    delete.alters_data = True

    def undelete(self, force_policy=None):
        """Undelete all soft deleted models.

        .. note::
            The current implementation loses performance on bulk undeletes in
            order to call the pre/post-save signals.

        .. seealso::
            :py:func:`safedelete.models.SafeDeleteModel.undelete`
        """
        assert self.query.can_filter(), "Cannot use 'limit' or 'offset' with undelete."
        # TODO: Replace this by bulk update if we can (need to call pre/post-save signal)
        for obj in self.all():
            obj.undelete(force_policy=force_policy)
        self._result_cache = None
    undelete.alters_data = True

    def all(self, force_visibility=None):
        """Override so related managers can also see the deleted models.

        A model's m2m field does not easily have access to `all_objects` and
        so setting `force_visibility` to True is a way of getting all of the
        models. It is not recommended to use `force_visibility` outside of related
        models because it will create a new queryset.

        Args:
            force_visibility: Force a deletion visibility. (default: {None})
        """
        if force_visibility is not None:
            self._safedelete_force_visibility = force_visibility
        return super(SafeDeleteQueryset, self).all()

    def _check_field_filter(self, **kwargs):
        """Check if the visibility for DELETED_VISIBLE_BY_FIELD needs t be put into effect.

        DELETED_VISIBLE_BY_FIELD is a temporary visibility flag that changes
        to DELETED_VISIBLE once asked for the named parameter defined in
        `_safedelete_force_visibility`. When evaluating the queryset, it will
        then filter on all models.
        """
        if self._safedelete_visibility == DELETED_VISIBLE_BY_FIELD \
                and self._safedelete_visibility_field in kwargs:
            self._safedelete_force_visibility = DELETED_VISIBLE

    def filter(self, *args, **kwargs):
        # Return a copy, see #131
        queryset = self._clone()
        queryset._check_field_filter(**kwargs)
        return super(SafeDeleteQueryset, queryset).filter(*args, **kwargs)

    def get(self, *args, **kwargs):
        # Return a copy, see #131
        queryset = self._clone()
        queryset._check_field_filter(**kwargs)
        # Filter visibility here because Django 3.0 adds a limit in get and we cannot filter afterward
        queryset._filter_visibility()
        return super(SafeDeleteQueryset, queryset).get(*args, **kwargs)

    def _filter_visibility(self):
        """Add deleted filters to the current QuerySet.

        Unlike QuerySet.filter, this does not return a clone.
        This is because QuerySet._fetch_all cannot work with a clone.
        """
        force_visibility = getattr(self, '_safedelete_force_visibility', None)
        visibility = force_visibility \
            if force_visibility is not None \
            else self._safedelete_visibility
        if not self._safedelete_filter_applied and \
           visibility in (DELETED_INVISIBLE, DELETED_VISIBLE_BY_FIELD, DELETED_ONLY_VISIBLE):
            assert self.query.can_filter(), \
                "Cannot filter a query once a slice has been taken."

            # Add a query manually, QuerySet.filter returns a clone.
            # QuerySet._fetch_all cannot work with clones.
            self.query.add_q(
                Q(
                    deleted__isnull=visibility in (
                        DELETED_INVISIBLE, DELETED_VISIBLE_BY_FIELD
                    )
                )
            )

            self._safedelete_filter_applied = True

    def __getitem__(self, key):
        """
        Override __getitem__ just before it hits the original queryset
        to apply the filter visibility method.
        """
        # get method add a limit in Django 3.0 and thus we can't filter here anymore in this case
        if self.query.can_filter:
            self._filter_visibility()

        return super(SafeDeleteQueryset, self).__getitem__(key)

    def __getattribute__(self, name):
        """Methods that do not return a QuerySet should call ``_filter_visibility`` first."""
        attr = object.__getattribute__(self, name)
        # These methods evaluate the queryset and therefore need to filter the
        # visiblity set.
        evaluation_methods = (
            '_fetch_all', 'count', 'exists', 'aggregate', 'update', '_update',
            'delete', 'undelete', 'iterator', 'first', 'last', 'latest', 'earliest'
        )
        if hasattr(attr, '__call__') and name in evaluation_methods:
            self._filter_visibility()

        return attr

    def _clone(self, klass=None, **kwargs):
        """Called by django when cloning a QuerySet."""
        if LooseVersion(django.get_version()) < LooseVersion('1.9'):
            clone = super(SafeDeleteQueryset, self)._clone(klass, **kwargs)
        else:
            clone = super(SafeDeleteQueryset, self)._clone(**kwargs)
        clone._safedelete_visibility = self._safedelete_visibility
        clone._safedelete_visibility_field = self._safedelete_visibility_field
        clone._safedelete_filter_applied = self._safedelete_filter_applied
        if hasattr(self, '_safedelete_force_visibility'):
            clone._safedelete_force_visibility = self._safedelete_force_visibility
        return clone


def get_lookup_value(obj, field):
    return reduce(lambda i, f: getattr(i, f), field.split(LOOKUP_SEP), obj)


class OrderedSafeDeleteQueryset(SafeDeleteQueryset):
    """
    # ADDED BY LEE

    This extends SafeDeleteQueryset with methods from OrderedModelQuerySet
    of the django-ordered-model package, so that we can have both proper ordering and
    safe-deletion
    """

    def _get_order_field_name(self):
        return self.model.order_field_name

    def _get_order_field_lookup(self, lookup):
        order_field_name = self._get_order_field_name()
        return LOOKUP_SEP.join([order_field_name, lookup])

    def _get_order_with_respect_to(self):
        model = self.model
        order_with_respect_to = model.order_with_respect_to
        if isinstance(order_with_respect_to, str):
            order_with_respect_to = (order_with_respect_to,)
        if order_with_respect_to is None:
            raise AssertionError(
                (
                    'ordered model admin "{0}" has not specified "order_with_respect_to"; note that this '
                    "should go in the model body, and is not to be confused with the Meta property of the same name, "
                    "which is independent Django functionality"
                ).format(model)
            )
        return order_with_respect_to

    def get_max_order(self):
        order_field_name = self._get_order_field_name()
        return self.aggregate(Max(order_field_name)).get(
            self._get_order_field_lookup("max")
        )

    def get_min_order(self):
        order_field_name = self._get_order_field_name()
        return self.aggregate(Min(order_field_name)).get(
            self._get_order_field_lookup("min")
        )

    def get_next_order(self):
        order = self.get_max_order()
        return order + 1 if order is not None else 0

    def above(self, order, inclusive=False):
        """Filter items above order."""
        lookup = "gte" if inclusive else "gt"
        return self.filter(**{self._get_order_field_lookup(lookup): order})

    def above_instance(self, ref, inclusive=False):
        """Filter items above ref's order."""
        order_field_name = self._get_order_field_name()
        order = getattr(ref, order_field_name)
        return self.above(order, inclusive=inclusive)

    def below(self, order, inclusive=False):
        """Filter items below order."""
        lookup = "lte" if inclusive else "lt"
        return self.filter(**{self._get_order_field_lookup(lookup): order})

    def below_instance(self, ref, inclusive=False):
        """Filter items below ref's order."""
        order_field_name = self._get_order_field_name()
        order = getattr(ref, order_field_name)
        return self.below(order, inclusive=inclusive)

    def decrease_order(self, **extra_kwargs):
        """Decrease `order_field_name` value by 1."""
        order_field_name = self._get_order_field_name()
        update_kwargs = {order_field_name: F(order_field_name) - 1}
        if extra_kwargs:
            update_kwargs.update(extra_kwargs)
        return self.update(**update_kwargs)

    def increase_order(self, **extra_kwargs):
        """Increase `order_field_name` value by 1."""
        order_field_name = self._get_order_field_name()
        update_kwargs = {order_field_name: F(order_field_name) + 1}
        if extra_kwargs:
            update_kwargs.update(extra_kwargs)
        return self.update(**update_kwargs)

    def bulk_create(self, objs, batch_size=None):
        order_field_name = self._get_order_field_name()
        order_with_respect_to = self.model.order_with_respect_to
        objs = list(objs)
        if order_with_respect_to:
            order_with_respect_to_mapping = {}
            order_with_respect_to = self._get_order_with_respect_to()
            for obj in objs:
                key = tuple(
                    get_lookup_value(obj, field) for field in order_with_respect_to
                )
                if key in order_with_respect_to_mapping:
                    order_with_respect_to_mapping[key] += 1
                else:
                    order_with_respect_to_mapping[
                        key
                    ] = self.filter_by_order_with_respect_to(obj).get_next_order()
                setattr(obj, order_field_name, order_with_respect_to_mapping[key])
        else:
            for order, obj in enumerate(objs, self.get_next_order()):
                setattr(obj, order_field_name, order)
        return super().bulk_create(objs, batch_size=batch_size)

    def _get_order_with_respect_to_filter_kwargs(self, ref):
        order_with_respect_to = self._get_order_with_respect_to()
        _get_lookup_value = partial(get_lookup_value, ref)
        return {field: _get_lookup_value(field) for field in order_with_respect_to}

    _get_order_with_respect_to_filter_kwargs.queryset_only = False

    def filter_by_order_with_respect_to(self, ref):
        order_with_respect_to = self.model.order_with_respect_to
        if order_with_respect_to:
            filter_kwargs = self._get_order_with_respect_to_filter_kwargs(ref)
            return self.filter(**filter_kwargs)
        return self