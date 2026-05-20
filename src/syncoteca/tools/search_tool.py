import os
import httpx
from crewai.tools import BaseTool
from pydantic import Field


class SearchRightsHolderTool(BaseTool):
    """Search for music rights holders via web and public databases."""

    name: str = "search_rights_holders"
    description: str = (
        "Search for music rights holders (composers, publishers, labels) by track name, "
        "artist name, ISRC, or ISWC. Returns contacts and rights split info."
    )

    def _run(self, query: str) -> str:
        api_key = os.getenv("SERPER_API_KEY")
        if not api_key:
            return self._mock_search(query)
        return self._serper_search(query, api_key)

    def _serper_search(self, query: str, api_key: str) -> str:
        try:
            resp = httpx.post(
                "https://google.serper.dev/search",
                headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
                json={"q": f"music rights holder publisher ISRC {query}", "num": 5},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            results = []
            for item in data.get("organic", [])[:5]:
                results.append(f"- {item.get('title')}: {item.get('snippet')}\n  URL: {item.get('link')}")
            return "\n".join(results) if results else "No results found."
        except Exception as e:
            return f"Search error: {e}"

    def _mock_search(self, query: str) -> str:
        return (
            f"[MOCK — добавьте SERPER_API_KEY для реального поиска]\n"
            f"Запрос: {query}\n\n"
            "Рекомендуемые ресурсы для поиска правообладателей:\n"
            "- ISRC Search: https://isrc.ifpi.org/search\n"
            "- ISWC Search: https://iswcnet.cisac.org\n"
            "- AllMusic: https://www.allmusic.com\n"
            "- Discogs: https://www.discogs.com\n"
            "- МузАрт (РАО): https://rao.ru\n"
            "- Musicbrainz: https://musicbrainz.org"
        )
