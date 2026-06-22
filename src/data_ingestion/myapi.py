import requests


class DataFetcher:
    def __init__(self, base_url):
        self.base_url = base_url
        self.headers = {"User-Agent": "Mozilla/5.0", "Content-Type": "application/json"}

    def fetch_get(self, endpoint, params=None):
        url = f"{self.base_url}/{endpoint}"

        response = requests.get(url, headers=self.headers, params=params)

        return self._handle_response(response)

    def fetch_post(self, endpoint, payload):
        url = f"{self.base_url}/{endpoint}"

        response = requests.post(url, headers=self.headers, json=payload)

        return self._handle_response(response)

    def _handle_response(self, response):
        if response.status_code == 200:
            return response.json()
        else:
            return f"Error {response.status_code}: {response.text}"


# ventual = myapi.DataFetcher("https://app.ventuals.com/api")
# hyper = myapi.DataFetcher("https://api.hyperliquid.xyz")
