# Community channels

Where Outerloop talks to the people using it, and what each channel carries.
Discord is the frequent channel: releases, merged work, and what the fleet is
doing. X and Bluesky are the public voice, for releases worth a sentence to
people who do not follow the repo; a human writes those.

## Discord

Server: [discord.gg/W5fuTFtJwF](https://discord.gg/W5fuTFtJwF), linked from
the landing page.

| channel | what goes there | source |
|---|---|---|
| `#announcements` | releases of `outerloop-science`, with the release notes | GitHub → Discord webhook on `outerloop-science/outerloop`, event **Releases** |
| `#dev` | pull requests on the kernel: opened, merged, closed | same webhook, event **Pull requests** |
| `#speedrun` | the agents' pull requests on `gpt-speedrun`: opened, merged, closed, with the title's metric change | GitHub → Discord webhook on the target repo, event **Pull requests** |
| `#help` | adopters' questions about install and setup | people |
| `#general` | everything else | people |

The two webhooks are GitHub's own Discord integration, so a message is always
a GitHub event and never a second copy of state. Setting one up, once per
channel:

1. Discord: Server Settings → Integrations → Webhooks → New Webhook, pick the
   channel, copy the webhook URL. The URL is a credential: it lets anyone post
   to that channel. It lives in GitHub's webhook settings and nowhere else.
2. GitHub: the repo's Settings → Webhooks → Add webhook. Payload URL is the
   Discord URL with `/github` appended; content type `application/json`;
   under "Let me select individual events" tick only the events in the table
   (Releases for `#announcements`, Pull requests for `#dev` and `#speedrun`).
   Leave pushes, issues and comments unticked, or the channel drowns.
3. Send the test payload GitHub offers and check the channel.

A target repo that wants its own channel repeats step 2 on that repo with a
webhook for its channel.

## X and Bluesky

`@outerloop_sci` on X and `@outerloop.science` on Bluesky, both linked from
the landing page. Posts are written by a person, for final releases and
results worth the attention of people who do not read the changelog; dev and
rc pre-releases go to Discord only. The release checklist in `RELEASING.md`
has the post as a step, so it is not left to memory.

## Later: the fleet posting its own wins

The kernel could emit typed events — an agent's pull request merged with its
metric change, a release published, the weekly digest — to config-driven
sinks, a Discord webhook first, so an adopter's fleet reports into their own
server without anyone reading GitHub notifications. One event type, several
sinks, each with a filter, so a new sink is a configuration line rather than a
second integration. Not built; it waits on the maintainer digest, which is the
content worth posting on a schedule.
