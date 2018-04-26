import json

from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
from django.db.models import Q
from django.conf import settings
from apps.ticket.models import TicketRecord, TicketCustomField, TicketFlowLog
from apps.workflow.models import CustomField
from service.account.account_base_service import AccountBaseService
from service.base_service import BaseService
from service.common.constant_service import CONSTANT_SERVICE
from service.common.log_service import auto_log
from service.workflow.workflow_base_service import WorkflowBaseService
from service.workflow.workflow_custom_field_service import WorkflowCustomFieldService
from service.workflow.workflow_state_service import WorkflowStateService
from service.workflow.workflow_transition_service import WorkflowTransitionService


class TicketBaseService(BaseService):
    """
    工单基础服务
    """
    def __init__(self):
        pass

    @classmethod
    @auto_log
    def get_ticket_list(cls, sn='', title='', username='', create_start='', create_end='', category='', reverse=1, per_page=10, page=1):
        """
        工单列表
        :param sn:
        :param title:
        :param username:
        :param create_start: 创建时间起
        :param create_end: 创建时间止
        :param category: 查询类别(创建的，待办的，关联的:包括创建的、处理过的、曾经需要处理但是没有处理的)
        :param reverse: 按照创建时间倒序
        :param per_page:
        :param page:
        :return:
        """
        category_list = ['all', 'owner', 'duty', 'relation']
        if category not in category_list:
            return False, '查询类别错误'

        query_params = Q(is_deleted=False)
        if sn:
            query_params &= Q(sn__startswith=sn)
        if title:
            query_params &= Q(title__contains=title)
        if create_start:
            query_params &= Q(gmt_created__gte=create_start)
        if create_end:
            query_params &= Q(gmt_created__lte=create_end)

        if reverse:
            order_by_str = '-gmt_created'
        else:
            order_by_str = 'gmt_created'

        if category == 'owner':
            query_params &= Q(creator=username)
        elif category == 'duty':
            # 获取用户部门id列表, 角色id列表，工单的实际当前处理人只会有个人、多人、角色、部门、脚本(变量、工单字段、父工单字段这些类型会在工单流转的时候计算为实际的值)
            user_obj, msg = AccountBaseService.get_user_by_username(username)
            if not user_obj:
                return False, msg
            user_dept_id_list, msg2 = AccountBaseService.get_user_up_dept_id_list(username)
            user_role_id_list, msg3 = AccountBaseService.get_user_role_id_list(username)
            if user_dept_id_list is False:
                return False, msg2
            if user_role_id_list is False:
                return False, msg3
            user_dept_id_str_list = [str(user_dept_id) for user_dept_id in user_dept_id_list]
            user_role_id_str_list = [str(user_role_id) for user_role_id in user_role_id_list]
            duty_query_expression = Q(participant_type_id=CONSTANT_SERVICE.PARTICIPANT_TYPE_PERSONAL, participant=username)
            duty_query_expression |= Q(participant_type_id=CONSTANT_SERVICE.PARTICIPANT_TYPE_DEPT, participant__in=user_dept_id_str_list)
            duty_query_expression |= Q(participant_type_id=CONSTANT_SERVICE.PARTICIPANT_TYPE_ROLE, participant__in=user_role_id_str_list)

        if category == 'duty':
            # 多人的情况，逗号隔开，需要用extra查询实现
            ticket_objects = TicketRecord.objects.filter(query_params).extra(where=['FIND_IN_SET({}, participant)'.format(username)]).order_by(order_by_str)
        elif category == 'relation':
            ticket_objects = TicketRecord.objects.filter(query_params).extra(where=['FIND_IN_SET({}, relation)'.format(username)]).order_by(order_by_str)
        else:
            ticket_objects = TicketRecord.objects.filter(query_params).order_by(order_by_str)

        paginator = Paginator(ticket_objects, per_page)


        try:
            ticket_result_paginator = paginator.page(page)
        except PageNotAnInteger:
            ticket_result_paginator = paginator.page(1)
        except EmptyPage:
            # If page is out of range (e.g. 9999), deliver last page of results
            ticket_result_paginator = paginator.page(paginator.num_pages)

        ticket_result_object_list = ticket_result_paginator.object_list
        ticket_result_restful_list = []
        for ticket_result_object in ticket_result_object_list:
            ticket_result_restful_list.append(dict(title=ticket_result_object.title,
                                                   workflow_id=ticket_result_object.workflow_id,
                                                   sn=ticket_result_object.sn,
                                                   state_id=ticket_result_object.state_id,
                                                   parent_ticket_id=ticket_result_object.parent_ticket_id,
                                                   parent_ticket_state_id=ticket_result_object.parent_ticket_state_id,
                                                   participant_type_id=ticket_result_object.participant_type_id,
                                                   participant=ticket_result_object.participant,
                                                   creator=ticket_result_object.creator,
                                                   gmt_created=str(ticket_result_object.gmt_created)[:19],
                                                   gmt_modified=str(ticket_result_object.gmt_modified)[:19],
                                                   ))
        return ticket_result_restful_list, dict(per_page=per_page, page=page, total=paginator.count)

    @classmethod
    @auto_log
    def new_ticket(cls, request_data_dict):
        """
        新建工单
        :param request_data_dict:
        :return:
        """
        transition_id = request_data_dict.get('transition_id')
        username = request_data_dict.get('username')
        workflow_id = request_data_dict.get('workflow_id')
        parent_ticket_id = request_data_dict.get('parent_ticket_id', 0)
        parent_ticket_state_id = request_data_dict.get('parent_ticket_state_id', 0)
        suggestion = request_data_dict.get('suggestion', '')
        if not (workflow_id and transition_id and username):
            return False, u'参数不合法,请提供workflow_id，username，transition_id'

        request_field_arg_list = [key for key, value in request_data_dict.items() if (key not in ['workflow_id', 'suggestion', 'username'])]

        # 判断用户是否有权限新建该工单
        has_permission, msg = WorkflowBaseService.checkout_new_permission(username, workflow_id)
        if not has_permission:
            return False, msg
        # 获取工单必填信息
        ## 获取工作流初始状态
        start_state, msg = WorkflowStateService.get_workflow_start_state(workflow_id)
        if not start_state:
            return False, msg
        # 获取初始状态必填字段 及允许更新的字段
        state_field_dict = json.loads(start_state.state_field_str)
        require_field_list, update_filed_list = [], []
        for key, value in state_field_dict.items():
            if value == CONSTANT_SERVICE.FIELD_ATTRIBUTE_REQUIRED:
                require_field_list.append(key)
            update_filed_list.append(key)
        # 校验是否所有必填字段都有提供
        for require_field in require_field_list:
            if require_field not in request_field_arg_list:
                return False, '此工单的必填字段为:{}'.format(','.join(request_field_arg_list))
        # 获取transition_id对应的下个状态的信息:
        transition_queryset, msg = WorkflowTransitionService.get_state_transition_queryset(start_state.id)
        if transition_queryset is False:
            return False, msg
        allow_transition_id_list = [transition.id for transition in transition_queryset]
        if transition_id not in allow_transition_id_list:
            return False, 'transition_id不合法'

        for transition_obj in transition_queryset:
            if transition_obj.id == transition_id:
                destination_state_id = transition_obj.destination_state_id
                break

        destination_state, msg = WorkflowStateService.get_workflow_state_by_id(destination_state_id)
        if not destination_state:
            return False, msg
        # 获取目标状态的信息
        destination_participant_type_id = destination_state.participant_type_id
        destination_participant = destination_state.participant
        if destination_participant_type_id == CONSTANT_SERVICE.PARTICIPANT_TYPE_FIELD:
            # 获取工单字段的值
            field_value, msg = cls.get_ticket_field_value(destination_participant)
            if not field_value:
                return False, msg
            destination_participant = field_value
            if len(field_value.split(',')) > 1:
                # 多人的情况
                destination_participant_type_id = CONSTANT_SERVICE.PARTICIPANT_TYPE_MULTI
        elif destination_participant_type_id == CONSTANT_SERVICE.PARTICIPANT_TYPE_PARENT_FIELD:

            destination_participant, msg = cls.get_ticket_field_value(parent_ticket_id, destination_participant)
            if len(destination_participant.split(',')) > 1:
                destination_participant_type_id = CONSTANT_SERVICE.PARTICIPANT_TYPE_FIELD
        elif destination_participant_type_id == CONSTANT_SERVICE.PARTICIPANT_TYPE_VARIABLE:
            if destination_participant == 'creator':
                destination_participant_type_id = CONSTANT_SERVICE.PARTICIPANT_TYPE_PERSONAL
                destination_participant = username
            elif destination_participant == 'creator_tl':
                # 获取用户的tl或审批人(优先审批人)
                approver, msg = AccountBaseService.get_user_dept_approver(username)
                if len(approver.split(',')) > 1:
                    destination_participant_type_id = CONSTANT_SERVICE.PARTICIPANT_TYPE_MULTI
                destination_participant = approver
        # 生成流水号
        ticket_sn, msg = cls.gen_ticket_sn(), msg
        if not ticket_sn:
            return False, msg
        # 新增工单基础表数据
        new_ticket_obj = TicketRecord(sn=ticket_sn, title=request_data_dict.get('title', ''), workflow_id=workflow_id,
                                      state_id=destination_state_id, parent_ticket_id=parent_ticket_id, parent_ticket_state_id=parent_ticket_state_id, participant=destination_participant,
                                      participant_type_id=destination_participant_type_id, relation=username, creator=username)
        new_ticket_obj.save()
        # 新增自定义字段，只保存required_field
        request_data_dict_allow = {}
        for key, value in request_data_dict:
            if key in require_field_list:
                request_data_dict_allow[key] = value

        update_ticket_custom_field_result, msg = cls.update_ticket_custom_field(new_ticket_obj.id, request_data_dict_allow)
        if not update_ticket_custom_field_result:
            return False, msg
        # 新增流转记录
        new_ticket_flow_log_dict = dict(ticket_id=new_ticket_obj.id, transition_id=transition_id, suggestion=suggestion,
                                        participant_type_id=CONSTANT_SERVICE.PARTICIPANT_TYPE_PERSONAL, participant=username,
                                        state_id=start_state.id)
        add_ticket_flow_log_result, msg = cls.add_ticket_flow_log(new_ticket_flow_log_dict)
        if not add_ticket_flow_log_result:
            return False, msg
        # 如果下个状态为脚本处理，则开始执行脚本
        if destination_participant_type_id == CONSTANT_SERVICE.PARTICIPANT_TYPE_ROBOT:
            pass
        return new_ticket_obj.id, ''

    @classmethod
    @auto_log
    def gen_ticket_sn(cls):
        redis_host = settings.REDIS_HOST
        redis_db = settings.REDIS_DB
        redis_port = settings.REDIS_PORT
        redis_password = settings.REDIS_PASSWORD
        import redis
        r = redis.Redis(host=redis_host, port=redis_port, db=redis_db, password=redis_password)
        import datetime
        ticket_day_count_key = 'ticket_day_count_{}'.format(str(datetime.datetime.now())[:10])
        ticket_day_count = r.get(ticket_day_count_key)
        if ticket_day_count is None:
            # 查询数据库中个数
            # 今天和明天
            today = str(datetime.datetime.now())[:10] + " 00:00:00"
            next_day = str(datetime.datetime.now() + datetime.timedelta(days=1))[:10] + " 00:00:00"
            # 包括is_deleted=1的数据

            ticket_day_count = TicketRecord.objects.filter(gmt_created__gte=today, gmt_created__lte=next_day).count()
        new_ticket_day_count = int(ticket_day_count) + 1
        r.set(ticket_day_count_key, new_ticket_day_count, 86400)
        now_day = datetime.datetime.now()
        return 'loonflow_%04d%02d%02d%04d' % (now_day.year, now_day.month, now_day.day, new_ticket_day_count), ''

    @classmethod
    @auto_log
    def get_ticket_field_value(cls, ticket_id, field_key):
        """
        获取工单字段的值
        :param ticket_id:
        :param field_key:
        :return:
        """
        #分为基础字段和自定义字段
        if field_key in CONSTANT_SERVICE.TICKET_BASE_FIELD_LIST:
            ticket_obj = TicketRecord.query.filter_by(id=ticket_id, is_deleted=0).first()
            ticket_obj_dict = ticket_obj.__dict__
            value = ticket_obj_dict.get(field_key)
        else:
            value, msg = cls.get_ticket_custom_field_value(ticket_id, field_key)
        return value, msg

    @classmethod
    @auto_log
    def get_ticket_custom_filed_value(cls, ticket_id, field_key):
        """
        获取工单的自定义字段的值
        :param ticket_id:
        :param field_key:
        :return:
        """
        ticket_obj = TicketRecord.query.filter_by(id=ticket_id, is_deleted=0).first()
        custom_field_queryset = CustomField.objects.filter(is_deleted=0, workflow_id=ticket_id.workflow_id).all()
        format_field_key_dict = {}
        for custom_field in custom_field_queryset:
            format_field_key_dict[custom_field.field_key] = dict(field_type_id=custom_field.field_type_id, name=custom_field.name, placeholder=custom_field.placeholder, bool_field_display=custom_field.bool_field_display,
                                                                     field_choice=custom_field.field_choice, filed_from='custom')
        field_type_id = format_field_key_dict[field_key]['field_type_id']
        ticket_custom_field_obj = TicketCustomField.query.filter_by(field_key=field_key, ticket_id=ticket_id, is_deleted=0).first()

        if not ticket_custom_field_obj:
            # 因为有可能该字段还没赋值
            value = None
        else:
            if field_type_id == CONSTANT_SERVICE.WORKFLOW_FIELD_TYPE_STR:
                value = ticket_custom_field_obj.value_char
            elif field_type_id == CONSTANT_SERVICE.WORKFLOW_FIELD_TYPE_INT:
                value = ticket_custom_field_obj.value_int
            elif field_type_id == CONSTANT_SERVICE.WORKFLOW_FIELD_TYPE_FLOAT:
                value = ticket_custom_field_obj.value_float
            elif field_type_id == CONSTANT_SERVICE.WORKFLOW_FIELD_TYPE_BOOL:
                value = ticket_custom_field_obj.value_bool
            elif field_type_id == CONSTANT_SERVICE.WORKFLOW_FIELD_TYPE_DATE:
                value = str(ticket_custom_field_obj.value_date)
            elif field_type_id == CONSTANT_SERVICE.WORKFLOW_FIELD_TYPE_DATETIME:
                value = str(ticket_custom_field_obj.value_datetime)
            elif field_type_id == CONSTANT_SERVICE.WORKFLOW_FIELD_TYPE_RADIO:
                value = ticket_custom_field_obj.value_radio
            elif field_type_id == CONSTANT_SERVICE.WORKFLOW_FIELD_TYPE_CHECKBOX:
                value = ticket_custom_field_obj.value_checkbox
            elif field_type_id == CONSTANT_SERVICE.WORKFLOW_FIELD_TYPE_SELECT:
                value = ticket_custom_field_obj.value_select
            elif field_type_id == CONSTANT_SERVICE.WORKFLOW_FIELD_TYPE_MULTI_SELECT:
                value = ticket_custom_field_obj.value_multi_select
            elif field_type_id == CONSTANT_SERVICE.WORKFLOW_FIELD_TYPE_TEXT:
                value = ticket_custom_field_obj.value_text
            elif field_type_id == CONSTANT_SERVICE.WORKFLOW_FIELD_TYPE_USERNAME:
                value = ticket_custom_field_obj.value_username
        return value, ''

    @classmethod
    @auto_log
    def update_ticket_custom_field(cls, ticket_id, update_dict):
        """
        更新工单自定义字段（新增或者修改）
        :param ticket_id:
        :param update_dict:
        :return:
        """
        # 获取工单的自定义字段
        ticket_obj = TicketRecord.objects.filter(id=ticket_id, is_deleted=0).first()
        format_custom_field_dict, msg = WorkflowCustomFieldService.get_workflow_custom_field(ticket_obj.workflow_id)
        if format_custom_field_dict is False:
            return False, msg
        custom_field_key_list = [key for key, value in format_custom_field_dict.items()]

        # 因为工单的自定义字段不会太多，且有可能是新增有可能是更新， 所以直接遍历处理
        for key, value in update_dict.items():
            if key in custom_field_key_list:
                # 判断是否存在，如果存在则更新，如果不存在则新增
                ticket_custom_filed_queryset = TicketCustomField.objects.filter(ticket_id=ticket_id, filed_key=key).all()
                field_type_id = format_custom_field_dict['key']['field_type_id']
                if ticket_custom_filed_queryset:
                    if field_type_id == CONSTANT_SERVICE.WORKFLOW_FIELD_TYPE_STR:
                        ticket_custom_filed_queryset['value_char'] = update_dict.get(key)
                    elif field_type_id == CONSTANT_SERVICE.WORKFLOW_FIELD_TYPE_INT:
                        ticket_custom_filed_queryset['value_int'] = update_dict.get(key)
                    elif field_type_id == CONSTANT_SERVICE.WORKFLOW_FIELD_TYPE_FLOAT:
                        ticket_custom_filed_queryset['value_float'] = update_dict.get(key)
                    elif field_type_id == CONSTANT_SERVICE.WORKFLOW_FIELD_TYPE_BOOL:
                        ticket_custom_filed_queryset['value_bool'] = int(update_dict.get(key))
                    elif field_type_id == CONSTANT_SERVICE.WORKFLOW_FIELD_TYPE_DATE:
                        ticket_custom_filed_queryset['value_date'] = update_dict.get(key)
                    elif field_type_id == CONSTANT_SERVICE.WORKFLOW_FIELD_TYPE_DATETIME:
                        ticket_custom_filed_queryset['value_datetime'] = update_dict.get(key)
                    elif field_type_id == CONSTANT_SERVICE.WORKFLOW_FIELD_TYPE_RADIO:
                        ticket_custom_filed_queryset['value_radio'] = update_dict.get(key)
                    elif field_type_id == CONSTANT_SERVICE.WORKFLOW_FIELD_TYPE_CHECKBOX:
                        ticket_custom_filed_queryset['value_checkbox'] = update_dict.get(key)
                    elif field_type_id == CONSTANT_SERVICE.WORKFLOW_FIELD_TYPE_SELECT:
                        ticket_custom_filed_queryset['value_select'] = update_dict.get(key)
                    elif field_type_id == CONSTANT_SERVICE.WORKFLOW_FIELD_TYPE_MULTI_SELECT:
                        ticket_custom_filed_queryset['value_multi_select'] = update_dict.get(key)
                    elif field_type_id == CONSTANT_SERVICE.WORKFLOW_FIELD_TYPE_TEXT:
                        ticket_custom_filed_queryset['value_text'] = update_dict.get(key)
                    ticket_custom_filed_queryset.save()
                else:
                    if field_type_id == CONSTANT_SERVICE.WORKFLOW_FIELD_TYPE_STR:
                        new_ticket_custom_field_record = TicketCustomField(value_char=update_dict.get(key))
                    elif field_type_id == CONSTANT_SERVICE.WORKFLOW_FIELD_TYPE_INT:
                        new_ticket_custom_field_record = TicketCustomField(value_int=update_dict.get(key))
                    elif field_type_id == CONSTANT_SERVICE.WORKFLOW_FIELD_TYPE_FLOAT:
                        new_ticket_custom_field_record = TicketCustomField(value_float=update_dict.get(key))
                    elif field_type_id == CONSTANT_SERVICE.WORKFLOW_FIELD_TYPE_BOOL:
                        new_ticket_custom_field_record = TicketCustomField(value_bool=update_dict.get(key))
                    elif field_type_id == CONSTANT_SERVICE.WORKFLOW_FIELD_TYPE_DATE:
                        new_ticket_custom_field_record = TicketCustomField(value_date=update_dict.get(key))
                    elif field_type_id == CONSTANT_SERVICE.WORKFLOW_FIELD_TYPE_DATETIME:
                        new_ticket_custom_field_record = TicketCustomField(value_datetime=update_dict.get(key))
                    elif field_type_id == CONSTANT_SERVICE.WORKFLOW_FIELD_TYPE_RADIO:
                        new_ticket_custom_field_record = TicketCustomField(value_radio=update_dict.get(key))
                    elif field_type_id == CONSTANT_SERVICE.WORKFLOW_FIELD_TYPE_CHECKBOX:
                        new_ticket_custom_field_record = TicketCustomField(value_checkbox=update_dict.get(key))
                    elif field_type_id == CONSTANT_SERVICE.WORKFLOW_FIELD_TYPE_SELECT:
                        new_ticket_custom_field_record = TicketCustomField(value_select=update_dict.get(key))
                    elif field_type_id == CONSTANT_SERVICE.WORKFLOW_FIELD_TYPE_MULTI_SELECT:
                        new_ticket_custom_field_record = TicketCustomField(value_multi_select=update_dict.get(key))
                    elif field_type_id == CONSTANT_SERVICE.WORKFLOW_FIELD_TYPE_TEXT:
                        new_ticket_custom_field_record = TicketCustomField(value_text=update_dict.get(key))
                    new_ticket_custom_field_record.save()
        return True, ''

    @classmethod
    @auto_log
    def update_ticket_field_value(cls, ticket_id, update_dict):
        """
        更新工单字段的值
        :param ticket_id:
        :param update_dict:
        :return:
        """
        return True, ''

    @classmethod
    @auto_log
    def add_ticket_flow_log(cls, kwargs):
        """
        新增工单流转记录
        :param kwargs:
        :return:
        """
        new_ticket_flow_log = TicketFlowLog(**kwargs)
        new_ticket_flow_log.save()
        return new_ticket_flow_log.id, ''

    @classmethod
    @auto_log
    def get_ticket_detail(cls, ticket_id, username):
        """
        获取工单详情,有处理权限，则按照当前状态返回对应的字段信息，只有查看权限则返回该工单对应工作流配置的展示字段信息
        :param ticket_id:
        :param username:
        :return:
        """
        handle_permission, msg = cls.ticket_handle_permission_check(ticket_id, username)
        if not handle_permission:
            view_permission, msg = cls.ticket_view_permission_check(ticket_id, username)
            if not view_permission:
                return False, msg
        ticket_obj = TicketRecord.objects.filter(id=ticket_id, is_deleted=0).first()

        # 工单基础字段及属性
        field_list = []
        field_list.append(dict(field_key='sn', name=u'流水号', value=ticket_obj.sn, order_id=0, field_type_id=CONSTANT_SERVICE.FIELD_TYPE_STR, field_attribute=CONSTANT_SERVICE.FIELD_ATTRIBUTE_RO))
        field_list.append(dict(field_key='title', name=u'标题', value=ticket_obj.title, order_id=20, field_type_id=CONSTANT_SERVICE.FIELD_TYPE_STR, field_attribute=CONSTANT_SERVICE.FIELD_ATTRIBUTE_RO))
        field_list.append(dict(field_key='state_id', name=u'状态id', value=ticket_obj.state_id, order_id=40, field_type_id=CONSTANT_SERVICE.FIELD_TYPE_STR, field_attribute=CONSTANT_SERVICE.FIELD_ATTRIBUTE_RO))
        field_list.append(dict(field_key='creator', name=u'创建人', value=ticket_obj.creator, order_id=80, field_type_id=CONSTANT_SERVICE.FIELD_TYPE_STR, field_attribute=CONSTANT_SERVICE.FIELD_ATTRIBUTE_RO))
        field_list.append(dict(field_key='created_at', name=u'创建时间', value=str(ticket_obj.gmt_created), order_id=100, field_type_id=CONSTANT_SERVICE.FIELD_TYPE_STR, field_attribute=CONSTANT_SERVICE.FIELD_ATTRIBUTE_RO))
        field_list.append(dict(field_key='updated_at', name=u'更新时间', value=str(ticket_obj.gmt_modified), order_id=120, field_type_id=CONSTANT_SERVICE.FIELD_TYPE_STR, field_attribute=CONSTANT_SERVICE.FIELD_ATTRIBUTE_RO))
        state_obj, msg = WorkflowStateService.get_workflow_state_by_id(ticket_obj.state_id)

        if not state_obj:
            return False, msg
        state_name = state_obj.name
        field_list.append(dict(field_key='state.state_name', name=u'状态', value=state_name, order_id=41, field_type_id=CONSTANT_SERVICE.FIELD_TYPE_STR, field_attribute=CONSTANT_SERVICE.FIELD_ATTRIBUTE_RO))

        # 工单所有自定义字段
        custom_filed_dict, msg = WorkflowCustomFieldService.get_workflow_custom_field(ticket_obj.workflow_id)
        for key, value in custom_filed_dict.items():
            field_type_id = value['field_type_id']
            ticket_custom_field_obj = TicketCustomField.objects.filter(filed_key=key, is_deleted=0).first()
            if not ticket_custom_field_obj:
                field_value = None  # 尚未赋值的情况
            else:
                #根据字段类型 获取对应列的值
                if field_type_id == CONSTANT_SERVICE.FIELD_TYPE_STR:
                    field_value = ticket_custom_field_obj.value_char
                elif field_type_id == CONSTANT_SERVICE.FIELD_TYPE_INT:
                    field_value = ticket_custom_field_obj.value_int
                elif field_type_id == CONSTANT_SERVICE.FIELD_TYPE_FLOAT:
                    field_value = ticket_custom_field_obj.value_float
                elif field_type_id == CONSTANT_SERVICE.FIELD_TYPE_BOOL:
                    field_value = ticket_custom_field_obj.value_bool
                elif field_type_id == CONSTANT_SERVICE.FIELD_TYPE_DATE:
                    field_value = str(ticket_custom_field_obj.value_date)
                elif field_type_id == CONSTANT_SERVICE.FIELD_TYPE_DATETIME:
                    field_value = str(ticket_custom_field_obj.value_datetime)
                elif field_type_id == CONSTANT_SERVICE.FIELD_TYPE_RADIO:
                    field_value = ticket_custom_field_obj.value_radio
                elif field_type_id == CONSTANT_SERVICE.FIELD_TYPE_CHECKBOX:
                    field_value = ticket_custom_field_obj.value_checkbox
                elif field_type_id == CONSTANT_SERVICE.FIELD_TYPE_SELECT:
                    field_value = ticket_custom_field_obj.value_select
                elif field_type_id == CONSTANT_SERVICE.FIELD_TYPE_MULTI_SELECT:
                    field_value = ticket_custom_field_obj.value_multi_select
                elif field_type_id == CONSTANT_SERVICE.FIELD_TYPE_TEXT:
                    field_value = ticket_custom_field_obj.value_text
                elif field_type_id == CONSTANT_SERVICE.FIELD_TYPE_USERNAME:
                    field_value = ticket_custom_field_obj.value_username

            field_list.append(dict(field_key=key, field_name=custom_filed_dict[key]['field_name'], field_value=field_value, order_id=custom_filed_dict[key]['order_id'],
                                   field_type_id=custom_filed_dict[key]['field_type_id'],
                                   field_attribute=CONSTANT_SERVICE.FIELD_ATTRIBUTE_RO,
                                   field_choice=json.loads(custom_filed_dict[key]['field_choice']),
                                   ))

        new_field_list = []

        if handle_permission:
            state_field_str = state_obj.state_field_str
            state_field_dict = json.loads(state_field_str)
            state_field_key_list = state_field_dict.keys()
            for field in field_list:
                if field['field_key'] in state_field_key_list:
                    field['field_attribute'] = state_field_dict[field['field_key']]
                    new_field_list.append(field)
        else:
            # 查看权限
            workflow_obj, msg = WorkflowBaseService.get_by_id(workflow_id=ticket_obj.workflow_id)
            display_form_field_list = json.loads(workflow_obj.display_form_str)
            for field in field_list:
                if field['field_key'] in display_form_field_list:
                    new_field_list.append(field)

        return dict(id=ticket_obj.id, sn=ticket_obj.sn, title=ticket_obj.title, state_id=ticket_obj.state_id, parent_ticket_id=ticket_obj.parent_ticket_id,
                    participant=ticket_obj.participant, participant_type_id=ticket_obj.participant_type_id, workflow_id=ticket_obj.workflow_id,
                    creator=ticket_obj.creator, gmt_created=str(ticket_obj.gmt_created), gmt_modified=str(ticket_obj.gmt_modified),
                    field_list=new_field_list), ''

    @classmethod
    @auto_log
    def ticket_handle_permission_check(cls, ticket_id, username):
        """
        处理权限校验: 获取当前状态是否需要处理， 该用户是否有权限处理
        :param ticket_id:
        :param username:
        :return:
        """
        ticket_obj = TicketRecord.objects.filter(id=ticket_id, is_deleted=0).first()
        if not ticket_obj:
            return False, '工单不存在或已被删除'
        ticket_state_id = ticket_obj.state_id
        transition_queryset, msg = WorkflowTransitionService.get_state_transition_queryset(ticket_state_id)
        if not transition_queryset:
            return False, '工单当前状态无需操作'
        state_obj, msg = WorkflowStateService.get_workflow_state_by_id(ticket_state_id)
        if not state_obj:
            return False, '工单当前状态id不存在或已被删除'
        participant_type_id = state_obj.participant_type_id
        participant = state_obj.participant
        if participant_type_id == CONSTANT_SERVICE.PARTICIPANT_TYPE_PERSONAL:
            if username != participant:
                return False, '非当前处理人，无权处理'
        elif participant_type_id == CONSTANT_SERVICE.PARTICIPANT_TYPE_MULTI:
            if username not in participant.split(','):
                return False, '非当前处理人，无权处理'
        elif participant_type_id == CONSTANT_SERVICE.PARTICIPANT_TYPE_DEPT:
            user_up_dept_id_list, msg = AccountBaseService.get_user_up_dept_id_list(username)
            if int(participant) not in user_up_dept_id_list:
                return False, '非当前处理人，无权处理'
        elif participant_type_id == CONSTANT_SERVICE.PARTICIPANT_TYPE_ROLE:
            user_role_id_list, msg = AccountBaseService.get_user_role_id_list(username)
            if int(participant) not in user_role_id_list:
                return False, '非当前处理人，无权处理'
        # PARTICIPANT_TYPE_VARIABLE, PARTICIPANT_TYPE_FIELD, PARTICIPANT_TYPE_PARENT_FIELD类型会在流转时保存为实际的处理人
        return True, ''

    @classmethod
    @auto_log
    def ticket_view_permission_check(cls, ticket_id, username):
        """
        校验用户是否有工单的查看权限:先查询对应的工作流是否校验查看权限， 如果不校验直接允许，如果校验需要判断用户是否属于工单的关系人
        :param ticket_id:
        :param username:
        :return:
        """
        ticket_obj = TicketRecord.objects.filter(id=ticket_id, is_deleted=0).first()
        if not ticket_obj:
            return False, '工单不存在或已被删除'
        workflow_obj, msg = WorkflowBaseService.get_by_id(ticket_obj.workflow_id)
        if not workflow_obj:
            return False, msg
        if not workflow_obj.view_permission_check:
            return True, '该工作流不限制查看权限'
        else:
            if username in ticket_obj.relation.split(','):
                return True, '用户是该工单的关系人，有查看权限'
            else:
                return False, '用户不是该工单的关系人，且该工作流开启了查看权限校验'

    @classmethod
    @auto_log
    def get_ticket_transition(cls, ticket_id, username):
        """
        获取用户针对工单当前可以做的操作:处理权限校验、可以做的操作
        :param ticket_id:
        :param username:
        :return:
        """
        handle_permission, msg = cls.ticket_handle_permission_check(ticket_id, username)
        if handle_permission is False:
            return False, msg
        if not handle_permission:
            return [], '用户当前无处理权限'
        ticket_obj = TicketRecord.objects.filter(id=ticket_id).first()
        transition_queryset, msg = WorkflowTransitionService.get_state_transition_queryset(ticket_obj.state_id)
        # if transition_queryset:
        transition_dict_list = []
        for transition in transition_queryset:
            transition_dict = dict(transition_id=transition.id, transition_name=transition.name)
            transition_dict_list.append(transition_dict)
        return transition_dict_list, ''

    @classmethod
    @auto_log
    def handle_ticket(cls, ticket_id, request_data_dict):
        """
        处理工单:校验必填参数,获取当前状态必填字段，更新工单基础字段，更新工单自定义字段， 更新工单流转记录，执行必要的脚本，通知消息
        此处逻辑和新建工单有较多重复，下个版本会拆出来
        :param ticket_id:
        :param request_data_dict:
        :return:
        """
        transition_id = request_data_dict.get('request_data_dict', '')
        username = request_data_dict.get('username', '')
        suggesition = request_data_dict.get('suggesition', '')

        if not (transition_id and username):
            return False, '参数不合法,请提供username，transition_id'
        ticket_obj = TicketRecord.objects.filter(id=ticket_id, is_deleted=False).first()
        source_ticket_state_id = ticket_obj.state_id
        if not ticket_obj:
            return False, '工单不存在或已被删除'

        # 判断用户是否有权限处理该工单
        has_permission, msg = cls.ticket_handle_permission_check(ticket_id, username)
        if not has_permission:
            return False, msg

        state_obj, msg = WorkflowStateService.get_workflow_state_by_id(ticket_obj.id)
        if not state_obj:
            return False, msg
        state_field_str = state_obj.state_field_str
        state_field_dict = json.load(state_field_str)
        require_field_list, update_filed_list = [], []
        for key, value in state_field_dict.items():
            if value == CONSTANT_SERVICE.FIELD_ATTRIBUTE_REQUIRED:
                require_field_list.append(key)
            update_filed_list.append(key)

        # 校验是否所有必填字段都有提供
        request_field_arg_list = [key for key, value in request_data_dict.items() if (key not in ['workflow_id', 'suggestion', 'username'])]
        for require_field in require_field_list:
            if require_field not in request_field_arg_list:
                return False, '此工单的必填字段为:{}'.format(','.join(request_field_arg_list))

        # 获取transition_id对应的下个状态的信息:
        transition_queryset, msg = WorkflowTransitionService.get_state_transition_queryset(ticket_obj.state_id)
        if transition_queryset is False:
            return False, msg
        allow_transition_id_list = [transition.id for transition in transition_queryset]
        if transition_id not in allow_transition_id_list:
            return False, 'transition_id不合法'

        for transition_obj in transition_queryset:
            if transition_obj.id == transition_id:
                destination_state_id = transition_obj.destination_state_id
                break

        destination_state, msg = WorkflowStateService.get_workflow_state_by_id(destination_state_id)
        if not destination_state:
            return False, msg
        # 获取目标状态的信息
        destination_participant_type_id = destination_state.participant_type_id
        destination_participant = destination_state.participant
        if destination_participant_type_id == CONSTANT_SERVICE.PARTICIPANT_TYPE_DEPT:
            username_list, msg = AccountBaseService.get_dept_username_list(int(destination_participant_type_id))
            add_relation = ','.join(username_list)

        elif destination_participant_type_id == CONSTANT_SERVICE.PARTICIPANT_TYPE_ROLE:
            username_list, msg = AccountBaseService.get_role_username_list(int(destination_participant))
            add_relation = ','.join(username_list)
        elif destination_participant_type_id == CONSTANT_SERVICE.PARTICIPANT_TYPE_FIELD:
            # 获取工单字段的值
            field_value, msg = cls.get_ticket_field_value(destination_participant)
            if not field_value:
                return False, msg
            destination_participant = field_value
            add_relation = destination_participant
            if len(field_value.split(',')) > 1:
                # 多人的情况
                destination_participant_type_id = CONSTANT_SERVICE.PARTICIPANT_TYPE_MULTI
        elif destination_participant_type_id == CONSTANT_SERVICE.PARTICIPANT_TYPE_PARENT_FIELD:
            destination_participant, msg = cls.get_ticket_field_value(ticket_obj.parent_ticket_id, destination_participant)
            if len(destination_participant.split(',')) > 1:
                destination_participant_type_id = CONSTANT_SERVICE.PARTICIPANT_TYPE_FIELD
            add_relation = destination_participant

        elif destination_participant_type_id == CONSTANT_SERVICE.PARTICIPANT_TYPE_VARIABLE:
            if destination_participant == 'creator':
                destination_participant_type_id = CONSTANT_SERVICE.PARTICIPANT_TYPE_PERSONAL
                destination_participant = username
            elif destination_participant == 'creator_tl':
                # 获取用户的tl或审批人(优先审批人)
                approver, msg = AccountBaseService.get_user_dept_approver(username)
                if len(approver.split(',')) > 1:
                    destination_participant_type_id = CONSTANT_SERVICE.PARTICIPANT_TYPE_MULTI
                destination_participant = approver
            add_relation = destination_participant

        # 更新工单信息：基础字段及自定义字段， add_relation字段 需要下个处理人是部门、角色等的情况
        new_relation = ','.join(set((ticket_obj.relation + add_relation).split(',')))  # 去重
        ticket_obj.participant_type_id = destination_participant_type_id
        ticket_obj.participant = destination_participant
        ticket_obj.relation = new_relation
        ticket_obj.save()

        # 只更新需要更新的字段
        request_update_dict = {}
        for key, value in request_data_dict.items():
            if key in update_filed_list:
                request_update_dict[key] = value


        update_ticket_custom_field_result, msg = cls.update_ticket_custom_field(ticket_id, request_update_dict)
        # 更新工单流转记录，执行必要的脚本，通知消息
        cls.add_ticket_flow_log(dict(ticket_id=ticket_id, transition_id=transition_id, suggesition=suggesition,
                                     participant_type_id=CONSTANT_SERVICE.PARTICIPANT_TYPE_PERSONAL, participant=username,
                                     state_id=source_ticket_state_id, creator=username))
        # 执行必要的脚本

        # 通知消息
        return True, ''

    @classmethod
    @auto_log
    def get_ticket_flow_log(cls, ticket_id, username, per_page=10, page=1):
        """
        获取工单流转记录
        :param ticket_id:
        :param username:
        :param per_page:
        :param page:
        :return:
        """
        ticket_flow_log_queryset = TicketFlowLog.objects.filter(ticket_id=ticket_id, is_deleted=0).all().order_by('-id')
        paginator = Paginator(ticket_flow_log_queryset, per_page)

        try:
            ticket_result_paginator = paginator.page(page)
        except PageNotAnInteger:
            ticket_result_paginator = paginator.page(1)
        except EmptyPage:
            # If page is out of range (e.g. 9999), deliver last page of results
            ticket_result_paginator = paginator.page(paginator.num_pages)

        ticket_flow_log_restful_list = []
        for ticket_flow_log in ticket_result_paginator.object_list:
            state_obj, msg = WorkflowStateService.get_workflow_state_by_id(ticket_flow_log.state_id)
            if ticket_flow_log.transition_id:
                transition_obj, msg = WorkflowTransitionService.get_workflow_transition_by_id(ticket_flow_log.transition_id)
                transition_name = transition_obj.name
            else:
                # 考虑到人工干预修改工单状态， transition_id为0
                transition_name = ''

            state_info_dict = dict(state_id=state_obj.id, state_name=state_obj.name)
            transition_info_dict = dict(transition_id=ticket_flow_log.transition_id, transition_name=transition_name)
            ticket_flow_log_restful_list.append(dict(ticket_id=ticket_id, state=state_info_dict, transition=transition_info_dict, suggestion=ticket_flow_log.suggestion,
                                                     gmt_created=str(ticket_flow_log.gmt_created)[:19], gmt_modified=str(ticket_flow_log.gmt_modified)[:19]
            ))

        return ticket_flow_log_restful_list, dict(per_page=per_page, page=page, total=paginator.count)

    @classmethod
    @auto_log
    def get_ticket_flow_step(cls, ticket_id, username):
        """
        工单的流转步骤，路径。直线流转, 步骤不会很多(因为同个状态只显示一次，隐藏的状态只有当前处于才显示，否则不显示)，默认先不分页
        :param ticket_id:
        :param username:
        :return:
        """
        # 先获取工单对应工作流的信息
        ticket_obj = TicketRecord.objects.filter(id=ticket_id, is_deleted=0).first()
        workflow_id = ticket_obj.workflow_id
        state_objs, msg = WorkflowStateService.get_workflow_states(workflow_id)
        ticket_flow_log_queryset = TicketFlowLog.objects.filter(ticket_id=ticket_id, is_deleted=0).all()

        state_step_dict_list = []
        for state_obj in state_objs:
            if state_obj.id == ticket_obj.state_id or (not state_obj.is_hidden):
                ticket_state_step_dict = dict(state_id=state_obj.id, state_name=state_obj.name)
                state_flow_log_list = []
                for ticket_flow_log in ticket_flow_log_queryset:
                    if ticket_flow_log.state_id == state_obj.id:
                        state_flow_log_list.append(dict(id=ticket_flow_log.id, suggestion=ticket_flow_log.suggestion, state_id=ticket_flow_log.state_id, gmt_created=str(ticket_flow_log.gmt_created)[:19]))
                ticket_state_step_dict['state_flow_log_list'] = state_flow_log_list
            state_step_dict_list.append(ticket_state_step_dict)
        return state_step_dict_list, ''

    @classmethod
    @auto_log
    def update_ticket_state(cls, ticket_id, state_id, username):
        """
        更新状态id,暂时只变更工单状态及工单当前处理人，不考虑目标状态状态处理人类型为脚本、变量、工单字段等等逻辑
        :param ticket_id:
        :param state_id:
        :param username:
        :return:
        """
        ticket_obj = TicketRecord.objects.filter(id=ticket_id, is_deleted=0).first()
        if not ticket_obj:
            return False, '工单不存在'
        source_state_id = ticket_obj.state_id
        state_obj, msg = WorkflowStateService.get_workflow_state_by_id(state_id)
        if not state_obj:
            return False, msg
        if state_obj.workflow_id == ticket_obj.workflow_id:
            ticket_obj.state_id = state_id
            ticket_obj.participant_type_id = state_obj.participant_type_id
            ticket_obj.participant = state_obj.participant
            ticket_obj.save()
            # 新增流转记录

            cls.add_ticket_flow_log(dict(ticket_id=ticket_id, transition_id=0, suggestion='强制修改工单状态', participant_type_id=CONSTANT_SERVICE.PARTICIPANT_TYPE_PERSONAL,
                                         participant=username, state_id=source_state_id))
            return True, '修改工单状态成功'

    @classmethod
    @auto_log
    def get_tickets_states_by_ticket_id_list(cls, ticket_id_list, username):
        """
        批量获取工单状态
        :param ticket_id_list:
        :param username:
        :return:
        """
        ticket_queryset = TicketRecord.objects.filter(id__in=ticket_id_list).all()
        ticket_state_id_list = [ticket.state_id for ticket in ticket_queryset]
        state_info_dict, msg = WorkflowStateService.get_states_info_by_state_id_list(ticket_state_id_list)
        return state_info_dict, msg




