from django.db.transaction import atomic
from django.db.models import Q
from django.utils.translation import ugettext_lazy as _
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.request import Request

from orgs.models import Organization
from orgs.mixins.api import OrgQuerySetMixin
from users.models.user import User
from common.const.http import POST, GET
from common.drf.api import JMSModelViewSet
from common.permissions import IsValidUser
from common.utils.django import get_object_or_none
from common.drf.serializers import EmptySerializer
from perms.models.asset_permission import AssetPermission, Asset
from assets.models.user import SystemUser
from ..exceptions import (
    ConfirmedAssetsChanged, ConfirmedSystemUserChanged,
    TicketClosed, TicketActionYet, NotHaveConfirmedAssets,
    NotHaveConfirmedSystemUser
)
from .. import serializers
from ..models import Ticket
from ..permissions import IsAssignee


class RequestAssetPermTicketViewSet(OrgQuerySetMixin, JMSModelViewSet):
    model = Ticket
    serializer_classes = {
        'default': serializers.RequestAssetPermTicketSerializer,
        'approve': EmptySerializer,
        'reject': EmptySerializer,
        'assignees': serializers.AssigneeSerializer,
    }
    permission_classes = (IsValidUser,)
    filter_fields = ['status', 'title', 'action', 'user_display', 'org_id']
    search_fields = ['user_display', 'title']

    def get_queryset(self):
        return super().get_queryset().filter(type=Ticket.TYPE_REQUEST_ASSET_PERM)

    def _check_can_set_action(self, instance, action):
        if instance.status == instance.STATUS_CLOSED:
            raise TicketClosed(detail=_('Ticket closed'))
        if instance.action == action:
            action_display = dict(instance.ACTION_CHOICES).get(action)
            raise TicketActionYet(detail=_('Ticket has %s') % action_display)

    @action(detail=False, methods=[GET], permission_classes=[IsValidUser])
    def assignees(self, request: Request, *args, **kwargs):
        user = request.user
        org_id = request.query_params.get('org_id', Organization.DEFAULT_ID)

        q = Q(role=User.ROLE_ADMIN)
        if not org_id == Organization.DEFAULT_ID:
            q |= (Q(related_admin_orgs__id=org_id) & (Q(related_admin_orgs__users=user) |
                                                      Q(related_admin_orgs__admins=user) |
                                                      Q(related_admin_orgs__auditors=user)))
        org_admins = User.objects.filter(q)

        page = self.paginate_queryset(org_admins)
        if page is not None:
            serializer = self.get_serializer(page, many=True)
            return self.get_paginated_response(serializer.data)

        serializer = self.get_serializer(org_admins, many=True)
        return Response(serializer.data)

    def _get_extra_comment(self, instance):
        meta = instance.meta
        ips = ', '.join(meta.get('ips', []))
        confirmed_assets = ', '.join(meta.get('confirmed_assets', []))

        return f'''
            {_('IP group')}: {ips}
            {_('Hostname')}: {meta.get('hostname', '')}
            {_('System user')}: {meta.get('system_user', '')}
            {_('Confirmed assets')}: {confirmed_assets}
            {_('Confirmed system user')}: {meta.get('confirmed_system_user', '')}
        '''

    @action(detail=True, methods=[POST], permission_classes=[IsAssignee, IsValidUser])
    def reject(self, request, *args, **kwargs):
        instance = self.get_object()
        action = instance.ACTION_REJECT
        self._check_can_set_action(instance, action)
        instance.perform_action(action, request.user, self._get_extra_comment(instance))
        return Response()

    @action(detail=True, methods=[POST], permission_classes=[IsAssignee, IsValidUser])
    def approve(self, request, *args, **kwargs):
        instance = self.get_object()
        action = instance.ACTION_APPROVE
        self._check_can_set_action(instance, action)

        meta = instance.meta
        confirmed_assets = meta.get('confirmed_assets', [])
        assets = list(Asset.objects.filter(id__in=confirmed_assets))
        if not assets:
            raise NotHaveConfirmedAssets(detail=_('Confirm assets first'))

        if len(assets) != len(confirmed_assets):
            raise ConfirmedAssetsChanged(detail=_('Confirmed assets changed'))

        confirmed_system_user = meta.get('confirmed_system_user')
        if not confirmed_system_user:
            raise NotHaveConfirmedSystemUser(detail=_('Confirm system-user first'))

        system_user = get_object_or_none(SystemUser, id=confirmed_system_user)
        if system_user is None:
            raise ConfirmedSystemUserChanged(detail=_('Confirmed system-user changed'))

        self._create_asset_permission(instance, assets, system_user, request.user)
        return Response({'detail': _('Succeed')})

    def _create_asset_permission(self, instance: Ticket, assets, system_user, user):
        meta = instance.meta
        request = self.request

        ap_kwargs = {
            'name': _('From request ticket: {} {}').format(instance.user_display, instance.id),
            'created_by': self.request.user.username,
            'comment': _('{} request assets, approved by {}').format(instance.user_display,
                                                                     instance.assignees_display)
        }
        date_start = meta.get('date_start')
        date_expired = meta.get('date_expired')
        if date_start:
            ap_kwargs['date_start'] = date_start
        if date_expired:
            ap_kwargs['date_expired'] = date_expired

        with atomic():
            instance.perform_action(instance.ACTION_APPROVE,
                                    request.user,
                                    self._get_extra_comment(instance))
            ap = AssetPermission.objects.create(**ap_kwargs)
            ap.system_users.add(system_user)
            ap.assets.add(*assets)
            ap.users.add(user)

        return ap
