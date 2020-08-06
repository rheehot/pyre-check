# (c) Facebook, Inc. and its affiliates. Confidential and proprietary.

from typing import Dict, List, Set, Tuple

import graphene
from graphene import relay
from graphene_sqlalchemy import get_session
from graphql.execution.base import ResolveInfo
from sqlalchemy.orm import Session, aliased
from sqlalchemy.sql import func

from .interactive import IssueQueryResult, IssueQueryResultType
from .models import (
    DBID,
    Issue,
    IssueInstance,
    IssueInstanceSharedTextAssoc,
    Run,
    RunStatus,
    SharedText,
    SharedTextKind,
    TraceKind,
)
from .query_builder import IssueQueryBuilder
from .trace_operator import (
    TraceFrameQueryResult,
    TraceFrameQueryResultType,
    TraceOperator,
)


FilenameText = aliased(SharedText)
CallableText = aliased(SharedText)
CallerText = aliased(SharedText)
CalleeText = aliased(SharedText)
MessageText = aliased(SharedText)


class IssueConnection(relay.Connection):
    class Meta:
        node = IssueQueryResultType


class TraceFrameConnection(relay.Connection):
    class Meta:
        node = TraceFrameQueryResultType


class Query(graphene.ObjectType):
    node = relay.Node.Field()
    issues = relay.ConnectionField(
        IssueConnection,
        codes=graphene.List(graphene.Int, default_value=["%"]),
        callables=graphene.List(graphene.String, default_value=["%"]),
        file_names=graphene.List(graphene.String, default_value=["%"]),
        trace_length_to_sinks=graphene.List(graphene.String, default_value=["%"]),
        trace_length_to_sources=graphene.List(graphene.String, default_value=["%"]),
    )
    trace = relay.ConnectionField(TraceFrameConnection, issue_id=graphene.ID())

    def resolve_issues(
        self,
        info: ResolveInfo,
        codes: List[int],
        callables: List[str],
        file_names: List[str],
        trace_length_to_sinks: List[str],
        trace_length_to_sources: List[str],
        **args
    ) -> List[IssueQueryResult]:
        session = get_session(info.context)
        run_id = Query.latest_run_id(session)

        builder = (
            IssueQueryBuilder(run_id)
            .with_session(session)
            .where_codes_is_any_of(codes)
            .where_callables_is_any_of(callables)
            .where_file_names_is_any_of(file_names)
        )

        return builder.get()

    def resolve_trace(
        self, info: ResolveInfo, issue_id: int
    ) -> List[TraceFrameQueryResult]:
        session = info.context.get("session")

        run_id = Query.latest_run_id(session)

        issue = (
            IssueQueryBuilder(run_id)
            .get_session_query(session)
            .filter(IssueInstance.id == issue_id)
            .join(Issue, IssueInstance.issue_id == Issue.id)
            .first()
        )

        leaf_kinds = Query.all_leaf_kinds(session)

        sources = Query._get_leaves_issue_instance(
            session,
            run_id,
            SharedTextKind.SOURCE,  # pyre-fixme[6] sqlalchemy enum (SharedTextKind) causing error
            leaf_kinds,
        )
        sinks = Query._get_leaves_issue_instance(
            session,
            run_id,
            SharedTextKind.SINK,  # pyre-fixme[6] sqlalchemy enum (SharedTextKind) causing error
            leaf_kinds,
        )

        postcondition_navigation = TraceOperator.navigate_trace_frames(
            leaf_kinds,
            session,
            run_id,
            sources,
            sinks,
            TraceOperator.initial_trace_frames(
                session, issue.id, TraceKind.POSTCONDITION
            ),
        )
        precondition_navigation = TraceOperator.navigate_trace_frames(
            leaf_kinds,
            session,
            run_id,
            sources,
            sinks,
            TraceOperator.initial_trace_frames(
                session, issue.id, TraceKind.PRECONDITION
            ),
        )

        trace_frames = (
            [frame_tuple[0] for frame_tuple in reversed(postcondition_navigation)]
            + [
                TraceFrameQueryResult(
                    id=DBID(0),
                    caller="",
                    caller_port="",
                    callee=issue.callable,
                    callee_port="root",
                    filename=issue.filename,
                    callee_location=issue.location,
                )
            ]
            + [frame_tuple[0] for frame_tuple in precondition_navigation]
        )

        return trace_frames

    @staticmethod
    def _get_leaves_issue_instance(
        session: Session,
        issue_instance_id: int,
        kind: SharedTextKind,
        leaf_kinds: Tuple[Dict[int, str], Dict[int, str], Dict[int, str]],
    ) -> Set[str]:
        message_ids = [
            int(id)
            for id, in session.query(SharedText.id)
            .distinct(SharedText.id)
            .join(
                IssueInstanceSharedTextAssoc,
                SharedText.id == IssueInstanceSharedTextAssoc.shared_text_id,
            )
            .filter(IssueInstanceSharedTextAssoc.issue_instance_id == issue_instance_id)
            .filter(SharedText.kind == kind)
        ]
        sources_dict, sinks_dict, features_dict = leaf_kinds
        return TraceOperator.leaf_dict_lookups(
            sources_dict, sinks_dict, features_dict, message_ids, kind
        )

    @staticmethod
    def all_leaf_kinds(
        session: Session,
    ) -> Tuple[Dict[int, str], Dict[int, str], Dict[int, str]]:
        return (
            {
                int(id): contents
                for id, contents in session.query(
                    SharedText.id, SharedText.contents
                ).filter(SharedText.kind == SharedTextKind.SOURCE)
            },
            {
                int(id): contents
                for id, contents in session.query(
                    SharedText.id, SharedText.contents
                ).filter(SharedText.kind == SharedTextKind.SINK)
            },
            {
                int(id): contents
                for id, contents in session.query(
                    SharedText.id, SharedText.contents
                ).filter(SharedText.kind == SharedTextKind.FEATURE)
            },
        )

    @staticmethod
    def latest_run_id(session: Session) -> int:
        return (
            session.query(func.max(Run.id))
            .filter(Run.status == RunStatus.FINISHED)
            .scalar()
        )


schema = graphene.Schema(query=Query, auto_camelcase=False)
