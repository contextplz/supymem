from huginn_watch.engine import Agent, Graph


class BaseHandler:
    type_name = "Agent"

    def check(self, agent: Agent, graph: Graph) -> None:
        return None

    def receive(self, agent: Agent, graph: Graph, events: list) -> None:
        return None

    def receive_web_request(
        self,
        agent: Agent,
        graph: Graph,
        *,
        secret: str,
        method: str,
        params: dict,
        headers: dict | None = None,
    ) -> tuple[str, int]:
        return ("not implemented", 404)
