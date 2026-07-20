from pymongo import MongoClient
from bson.objectid import ObjectId
from typing import List, Dict, Optional
from app.utils.logger_config import setup_logging, log_api_request
logger = setup_logging(app_name="kubeintellect")


class MongoChatDB:
    def __init__(self, host="localhost", port=27017, db_name="LibreChat"):
        self.client = MongoClient(host, port)
        self.db = self.client[db_name]

    def list_collections(self) -> List[str]:
        return self.db.list_collection_names()

    def collection_stats(self) -> Dict[str, int]:
        return {name: self.db[name].count_documents({}) for name in self.list_collections()}

    # ----- USERS -----
    def list_users(self, limit=10) -> List[Dict]:
        return list(self.db.users.find().limit(limit))

    def get_user_by_email(self, email: str) -> Optional[Dict]:
        return self.db.users.find_one({"email": email})

    # ----- CONVERSATIONS -----
    def list_conversations(self, limit=10) -> List[Dict]:
        return list(self.db.conversations.find().limit(limit))

    def get_conversations_by_user(self, user_id: ObjectId, limit=20) -> List[Dict]:
        # Try both 'user' and 'userId' fields for compatibility
        return list(self.db.conversations.find({"$or": [{"user": user_id}, {"userId": user_id}]}).limit(limit))

    def get_conversation_by_id(self, conversation_id: str) -> Optional[Dict]:
        return self.db.conversations.find_one({"conversationId": conversation_id})

    # ----- MESSAGES -----
    def list_messages(self, limit=10) -> List[Dict]:
        return list(self.db.messages.find().limit(limit))

    def get_messages_by_conversation(self, conversation_id: str, limit=50) -> List[Dict]:
        return list(self.db.messages.find({"conversationId": conversation_id}).sort("createdAt", 1).limit(limit))

    def get_messages_by_user(self, user_id: ObjectId, limit=50) -> List[Dict]:
        return list(self.db.messages.find({"user": user_id}).sort("createdAt", 1).limit(limit))

    # ----- UTILITIES -----
    def schema_sample(self, col_name: str, samples=5) -> Dict[str, List]:
        """Return all keys and up to 3 example values per key for a given collection."""
        from collections import defaultdict
        field_map = defaultdict(list)
        sample_docs = list(self.db[col_name].find().limit(samples))
        for doc in sample_docs:
            for key, val in doc.items():
                if len(field_map[key]) < 3:
                    field_map[key].append(val)
        return field_map

    def close(self):
        self.client.close()



# def get_memory(db, conversation_id: str, N: int = 10):
#     """
#     Fetch the last N messages for a conversation, sorted oldest to newest,
#     and return in OpenAI-compatible format.
#     """
#     # Fetch messages sorted by createdAt, get the last N
#     cursor = db.messages.find(
#         {"conversationId": conversation_id}
#     ).sort("createdAt", -1).limit(N)
#     msgs = list(cursor)
#     # Reverse to get chronological order
#     msgs = msgs[::-1]
#     # Format for LLM: [{"role": ..., "content": ...}]
#     chat_history = []

#     logger.info(f"Fetching {N} messages for conversation {conversation_id}")
#     logger.info(f"Messages: {msgs}")
#     for m in msgs:
#         # Determine role (based on 'isCreatedByUser' or 'sender')
#         if m.get("isCreatedByUser", False) or m.get("sender", "").lower() == "user":
#             role = "user"
#         else:
#             role = "assistant"
#         # Fallback to 'text' (LibreChat) or 'content' (if you ever add that)
#         content = m.get("text") or m.get("content", "")
#         chat_history.append({"role": role, "content": content})
#     logger.info(f"Chat history: {chat_history}")
#     return chat_history




# ------------- Example Usage -------------
if __name__ == "__main__":
    # For in-cluster: host="mongodb", for external/port-forward: host="localhost"
    chatdb = MongoChatDB(host="mongodb.kubeintellect.svc.cluster.local", db_name="LibreChat")

    print("Collections:", chatdb.list_collections())
    print("Collection stats:", chatdb.collection_stats())

    print("\nSample users:")
    for user in chatdb.list_users():
        print(user)

    print("\nSample conversations:")
    for conv in chatdb.list_conversations():
        print(conv)

    print("\nSample messages:")
    for msg in chatdb.list_messages():
        print(msg)

    # Schema sample
    print("\nUser collection schema sample:")
    print(chatdb.schema_sample("users", samples=5))

    chatdb.close()
