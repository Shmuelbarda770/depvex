import requests

url = "https://pypi.org/simple/"

response = requests.get(url)
response.raise_for_status()

packages = []

for line in response.text.splitlines():
    if "href=" in line:
        name = line.split(">")[1].split("<")[0]
        packages.append(name)

with open("pypi_packages.txt", "w", encoding="utf-8") as f:
    for package in packages:
        f.write(package + "\n")
