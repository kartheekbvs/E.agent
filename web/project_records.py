
from uuid import uuid4

from web.supabase_store import get_supabase


def create_chat(user_id: str | int, title: str = "New Chat") -> dict:
    """Create a chat owned by the authenticated Entropy user."""

    if user_id is None or not str(user_id).strip():
        raise ValueError("Authenticated user ID is required")

    result = (
        get_supabase()
        .table("chats")
        .insert({
            "user_id": str(user_id),
            "title": title.strip() or "New Chat",
        })
        .execute()
    )

    if not result.data:
        raise RuntimeError("Chat creation was not confirmed")

    return result.data[0]


def update_chat_title(
    user_id: str | int,
    chat_id: str,
    title: str,
) -> dict:
    """Update a chat title for the authenticated user only."""

    if user_id is None or not str(user_id).strip():
        raise ValueError("Authenticated user ID is required")

    cleaned = (title or "").strip()
    if not cleaned:
        raise ValueError("Chat title cannot be empty")
    if len(cleaned) > 80:
        cleaned = cleaned[:80].rstrip()

    result = (
        get_supabase()
        .table("chats")
        .update({"title": cleaned})
        .eq("id", chat_id)
        .eq("user_id", str(user_id))
        .execute()
    )

    if not result.data:
        raise PermissionError("Chat not found or access denied")

    return result.data[0]


def create_project(
    user_id: str | int,
    chat_id: str,
    summary: str | None = None,
) -> dict:
    """Create a project after verifying ownership of its chat."""

    if user_id is None or not str(user_id).strip():
        raise ValueError("Authenticated user ID is required")

    client = get_supabase()
    owner_id = str(user_id)

    chats = (
        client.table("chats")
        .select("id")
        .eq("id", chat_id)
        .eq("user_id", owner_id)
        .limit(1)
        .execute()
    )

    if not chats.data:
        raise PermissionError(
            "Chat not found or does not belong to this user"
        )

    project_id = str(uuid4())
    storage_prefix = f"{owner_id}/{chat_id}/{project_id}"

    result = (
        client.table("projects")
        .insert({
            "id": project_id,
            "chat_id": chat_id,
            "user_id": owner_id,
            "summary": summary,
            "storage_prefix": storage_prefix,
        })
        .execute()
    )

    if not result.data:
        raise RuntimeError("Project creation was not confirmed")

    return result.data[0]


def get_project_for_user(
    user_id: str | int,
    project_id: str,
) -> dict:
    """Return a project only if it belongs to the authenticated user."""

    if user_id is None or not str(user_id).strip():
        raise ValueError("Authenticated user ID is required")

    result = (
        get_supabase()
        .table("projects")
        .select("*")
        .eq("id", project_id)
        .eq("user_id", str(user_id))
        .limit(1)
        .execute()
    )

    if not result.data:
        raise PermissionError(
            "Project not found or access denied"
        )

    return result.data[0]


def list_chats_for_user(user_id: str | int) -> list[dict]:
    """Return chats belonging to the authenticated user, newest first."""

    if user_id is None or not str(user_id).strip():
        raise ValueError("Authenticated user ID is required")

    result = (
        get_supabase()
        .table("chats")
        .select("*")
        .eq("user_id", str(user_id))
        .order("created_at", desc=True)
        .execute()
    )
    return result.data or []


def list_projects_for_chat(
    user_id: str | int,
    chat_id: str,
) -> list[dict]:
    """Return projects for a chat, but only when the chat belongs to the user."""

    if user_id is None or not str(user_id).strip():
        raise ValueError("Authenticated user ID is required")

    client = get_supabase()
    owner_id = str(user_id)

    chat_result = (
        client.table("chats")
        .select("id")
        .eq("id", chat_id)
        .eq("user_id", owner_id)
        .limit(1)
        .execute()
    )

    if not chat_result.data:
        raise PermissionError("Chat not found or access denied")

    result = (
        client.table("projects")
        .select("*")
        .eq("chat_id", chat_id)
        .eq("user_id", owner_id)
        .execute()
    )
    return result.data or []
