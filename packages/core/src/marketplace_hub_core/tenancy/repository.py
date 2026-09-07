from __future__ import annotations

from sqlalchemy import Engine, and_, exists, select

from marketplace_hub_core.auth.schema import auth_users
from marketplace_hub_core.tenancy.schema import (
    membership_permissions,
    membership_seller_access,
    memberships,
    organization_relationships,
    organizations,
    roles,
    seller_profiles,
    workspace_selections,
)


class SqlWorkspaceRepository:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def grants(self, user_id) -> list[dict]:
        query = (
            select(memberships, organizations.c.name.label("organization_name"), roles.c.label)
            .join(organizations, memberships.c.organization_id == organizations.c.id)
            .join(roles, memberships.c.role_code == roles.c.code)
            .join(auth_users, memberships.c.user_id == auth_users.c.id)
            .where(
                memberships.c.user_id == user_id,
                memberships.c.active.is_(True),
                organizations.c.active.is_(True),
                auth_users.c.active.is_(True),
            )
        )
        with self.engine.connect() as connection:
            grants = [dict(row) for row in connection.execute(query).mappings()]
            if not grants:
                return []
            permission_rows = connection.execute(
                select(membership_permissions).where(
                    membership_permissions.c.membership_id.in_([item["id"] for item in grants])
                )
            ).mappings()
            by_membership: dict = {}
            for row in permission_rows:
                by_membership.setdefault(row["membership_id"], set()).add(row["permission_code"])
            for grant in grants:
                grant["permissions"] = by_membership.get(grant["id"], set())
            return grants

    def sellers_for_grant(self, grant: dict) -> list[dict]:
        query = (
            select(seller_profiles, organizations.c.name.label("organization_name"))
            .join(organizations, seller_profiles.c.organization_id == organizations.c.id)
            .where(seller_profiles.c.active.is_(True), organizations.c.active.is_(True))
        )
        kind = grant["organization_kind"]
        if kind == "SELLER":
            query = query.where(seller_profiles.c.organization_id == grant["organization_id"])
        elif kind == "AGENCY":
            query = query.where(
                exists(
                    select(1)
                    .select_from(organization_relationships)
                    .where(
                        organization_relationships.c.parent_id == grant["organization_id"],
                        organization_relationships.c.child_id == seller_profiles.c.organization_id,
                        organization_relationships.c.active.is_(True),
                    )
                )
            )
        elif kind != "PLATFORM":
            return []
        if not grant["all_sellers"]:
            query = query.where(
                exists(
                    select(1)
                    .select_from(membership_seller_access)
                    .where(
                        membership_seller_access.c.membership_id == grant["id"],
                        membership_seller_access.c.seller_id == seller_profiles.c.id,
                    )
                )
            )
        with self.engine.connect() as connection:
            return [dict(row) for row in connection.execute(query).mappings()]

    def platform_organizations(self, grant: dict) -> list[dict]:
        if grant["organization_kind"] != "PLATFORM" or not grant["all_sellers"]:
            return []
        with self.engine.connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    select(organizations).where(organizations.c.active.is_(True))
                ).mappings()
            ]

    def selected_seller(self, session_id):
        with self.engine.connect() as connection:
            return connection.scalar(
                select(workspace_selections.c.seller_id).where(
                    workspace_selections.c.session_id == session_id
                )
            )

    def save_selection(self, session_id, seller_id) -> None:
        if self.engine.dialect.name == "postgresql":
            from sqlalchemy.dialects.postgresql import insert
        else:
            from sqlalchemy.dialects.sqlite import insert
        statement = insert(workspace_selections).values(session_id=session_id, seller_id=seller_id)
        statement = statement.on_conflict_do_update(
            index_elements=[workspace_selections.c.session_id], set_={"seller_id": seller_id}
        )
        with self.engine.begin() as connection:
            connection.execute(statement)

    def clear_selection(self, session_id) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                workspace_selections.delete().where(workspace_selections.c.session_id == session_id)
            )

    def profile_in_scope(self, seller_id, organization_id) -> dict | None:
        """Domain reads use both identifiers from an authorized scope, never browser IDs alone."""
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    select(seller_profiles).where(
                        and_(
                            seller_profiles.c.id == seller_id,
                            seller_profiles.c.organization_id == organization_id,
                            seller_profiles.c.active.is_(True),
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            return dict(row) if row else None
