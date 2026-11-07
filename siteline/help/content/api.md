---
title: API tokens
pages: api_settings api_docs api_token_detail
who: staff
order: 91
---
Let another system, such as Power BI or your accounts package, read your jobs. It can only read, never change anything.

1. **Make a token** for each system. Name it and tick only what it needs. Money and incidents have to be ticked.
2. **Copy the token straight away.** It is shown once. If it's lost, revoke it and make another.
3. **Give it to whoever sets up the other system**, with *How to use the API*. Don't email it.
4. **Revoke a token** as soon as it isn't needed, or if it may have got out. It stops at once.

A token reads every job that isn't archived, as staff would, and stops if its maker is no longer an owner or admin. Every call is logged on the token's page.
