"""Source-owned negative admission proof; never inferred from provider prose."""


class ResearchPreflightBlocked(RuntimeError):
    """Research admission rejected before any durable mutation or adopted job."""


SEARCH_KEY_FIELDS = {"tavily": "Search API key for Tavily", "exa": "Search API key for Exa",
                     "firecrawl": "Firecrawl key", "openai": "OpenAI API key"}


class SearchKeyRejected(ValueError):
    def __init__(self, provider: str, status: int):
        self.status = status
        super().__init__("research_search_key_" + provider)
