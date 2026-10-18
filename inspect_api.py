
import requests
import json

URL = "https://exam.sanand.workers.dev/questionData"

PARAMS = {
    "email": "23f1000183@ds.study.iitm.ac.in",
    "quizSign": "hNYxrUzlTTboRJkV6J/ubuJwOnoyij9p5puwOsPcDUIkTEuWOVK79Ir24ywxZpEsUk8fpT4OFg1EFAN7qdvjn0aDnlKuAAuSZXzgrNpshjw37g1FErEg3CHoShNk5Hu75Lwz9AeZP5IJ0FvPli285a7l7hqNtjSeH9D1HefU1hSFyVFP7tv+XRRVQVfqPVLfEEAuqKwEldIZ7ztiOJjrGVTEzRUE4sk6Hu5NykUsRLA1M0zBfLPqbq2gfrcNdS/mAioxecK445weKuk2oJ0Ckf9WCUfsysaUPu7J6NsRTGjd/mV9QWj9ZgWHgan1VrTQb0PmMoo1TApoqryKNNwSRg==",
    "questionId": "q-ledger-agent-server",
    "path": "/export",
}

try:
    response = requests.get(URL, params=PARAMS, timeout=60)

    print("Status:", response.status_code)
    print("Content-Type:", response.headers.get("Content-Type"))

    if response.status_code == 200:
        lines = [
            line for line in response.text.splitlines()
            if line.strip()
        ]

        print("Total exported order rows:", len(lines))

        if lines:
            print("\nFirst order:")
            print(json.dumps(json.loads(lines[0]), indent=2))

            print("\nLast order:")
            print(json.dumps(json.loads(lines[-1]), indent=2))
    else:
        print("\nError response:")
        print(response.text[:2000])

except requests.RequestException as error:
    print("Request failed:", error)
except json.JSONDecodeError as error:
    print("Could not parse an order as JSON:", error)
