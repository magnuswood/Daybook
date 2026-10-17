# The Daybook

Personal news page built hourly from free RSS feeds (BBC, Guardian, The Conversation, Personnel Today, independent writers, Google News searches with paywalled publishers removed).

- build.py fetches feeds, embeds resized images, writes one self-contained page
- template.html is the page design
- .github/workflows/build.yml rebuilds hourly and publishes to GitHub Pages
