"""HTML for newsletter invitations and the weekly Regulatory Brief."""

# ruff: noqa: E501

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from app.services.notification_service import html_text

PUBLIC_NEWSLETTER_URL = 'https://reg-ai-production.vercel.app/newsletter'


def build_newsletter_invite_email_html(
    *,
    accept_url: str,
    organization_name: str | None,
    invitee_email: str,
    public_signup: bool = False,
) -> str:
    org_label = html_text(organization_name or 'a Reg-AI organization')
    safe_url = html_text(accept_url)
    safe_email = html_text(invitee_email)
    if public_signup:
        intro = (
            'Confirm you want the <strong>Reg-AI Newsletter</strong> at this address. '
            'We will not add you until you accept this invitation.'
        )
    else:
        intro = (
            f'A colleague at <strong style="color:#0f172a;">{org_label}</strong> thought you would '
            'value the <strong>Reg-AI Newsletter</strong>, a Monday briefing built from the same '
            'Regulatory Feed we use to watch new government publications.'
        )
    return f"""\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>You're invited to the Reg-AI Newsletter</title>
</head>
<body
  style="margin:0;padding:0;background:#eef2f7;color:#0f172a;
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;"
>
  <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#eef2f7;padding:32px 16px;">
    <tr>
      <td align="center">
        <table role="presentation" width="100%" cellspacing="0" cellpadding="0"
          style="max-width:560px;background:#ffffff;border-radius:16px;overflow:hidden;box-shadow:0 10px 30px rgba(15,23,42,0.08);">
          <tr>
            <td style="background:linear-gradient(135deg,#0b1f3a 0%,#12315a 55%,#1d4f91 100%);padding:28px 32px;">
              <p style="margin:0;font-size:12px;letter-spacing:0.18em;text-transform:uppercase;color:#93c5fd;font-weight:600;">Reg AI</p>
              <h1 style="margin:10px 0 0;font-size:24px;line-height:1.25;color:#ffffff;font-weight:700;">You're invited to the weekly brief</h1>
              <p style="margin:8px 0 0;font-size:14px;line-height:1.5;color:#dbeafe;">SEC, FINRA, and CFTC — summarized for CCOs</p>
            </td>
          </tr>
          <tr>
            <td style="padding:32px;">
              <p style="margin:0 0 16px;font-size:16px;line-height:1.6;">Hello,</p>
              <p style="margin:0 0 16px;font-size:15px;line-height:1.6;color:#334155;">
                {intro}
              </p>
              <p style="margin:0 0 20px;font-size:15px;line-height:1.6;color:#334155;">
                Each week you get AI summaries of SEC, FINRA, and CFTC releases from the prior seven
                days: what the agency announced, who is affected, and why a compliance team should care.
                It is free to subscribe at <strong style="color:#0f172a;">{safe_email}</strong>.
              </p>
              <table role="presentation" cellspacing="0" cellpadding="0" style="margin:0 0 20px;">
                <tr>
                  <td align="center" bgcolor="#0b1f3a" style="border-radius:10px;">
                    <a href="{safe_url}" style="display:inline-block;padding:14px 28px;font-size:15px;font-weight:600;color:#ffffff;text-decoration:none;">
                      Accept invitation
                    </a>
                  </td>
                </tr>
              </table>
              <p style="margin:0;font-size:12px;line-height:1.6;color:#94a3b8;">
                If the button doesn't work, paste this link into your browser:<br />
                <a href="{safe_url}" style="color:#1d4f91;word-break:break-all;">{safe_url}</a>
              </p>
            </td>
          </tr>
          <tr>
            <td style="padding:18px 32px 24px;border-top:1px solid #e2e8f0;background:#f8fafc;">
              <p style="margin:0;font-size:12px;line-height:1.5;color:#94a3b8;">
                This invitation was sent by Reg-AI. If you weren't expecting it, you can ignore this email.
              </p>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>
"""


def classify_publication_kind(title: str) -> str:
    text = (title or '').lower()
    if 'propos' in text:
        return 'Proposed rule'
    if any(token in text for token in ('charg', 'enforc', 'settle', 'ponzi', 'fraud')):
        return 'Enforcement'
    if any(token in text for token in ('roundtable', 'meeting', 'agenda', 'panel')):
        return 'Event'
    if 'notice' in text:
        return 'Regulatory Notice'
    if 'filing' in text or 'sr-finra' in text:
        return 'Rule filing'
    if 'podcast' in text:
        return 'Podcast'
    return 'Agency action'


def looks_like_comment_period(title: str, summary: str) -> bool:
    blob = f'{title} {summary}'.lower()
    return 'comment' in blob and any(token in blob for token in ('due', 'period', 'close'))


def format_brief_date(value: datetime | None) -> str:
    if value is None:
        return ''
    return f'{value.strftime("%b")} {value.day}, {value.year}'


def _summary_paragraphs(text: str) -> list[str]:
    cleaned = (text or '').strip()
    if not cleaned:
        return []
    blocks = [block.strip() for block in cleaned.split('\n\n') if block.strip()]
    if len(blocks) > 1:
        return [' '.join(block.split()) for block in blocks]
    return [' '.join(cleaned.split())]


def build_summary_html(summary: str) -> str:
    """Render the full Regulatory Feed SUMMARY. Gmail cannot honor in-email toggles."""
    paragraphs = _summary_paragraphs(summary)
    if not paragraphs:
        return ''
    body_style = (
        'margin:0 0 10px;font-size:14px;line-height:1.6;color:#334155;'
        'overflow-wrap:anywhere;word-break:break-word;'
    )
    last_style = body_style.replace('margin:0 0 10px', 'margin:0 0 12px')
    parts: list[str] = []
    for index, paragraph in enumerate(paragraphs):
        style = last_style if index == len(paragraphs) - 1 else body_style
        parts.append(f'<p style="{style}">{html_text(paragraph)}</p>')
    return ''.join(parts)


def build_expandable_summary_html(summary: str, *, toggle_id: str = '') -> str:
    """Compatibility wrapper — Gmail strips Show more/less, so the full summary is inlined."""
    _ = toggle_id
    return build_summary_html(summary)


def build_weekly_newsletter_html(
    *,
    week_ending: datetime,
    items: Sequence[dict[str, str]],
    signup_url: str,
    read_more_url: str = PUBLIC_NEWSLETTER_URL,
) -> str:
    ending_label = f'{week_ending.strftime("%B")} {week_ending.day}, {week_ending.year}'
    sec = [item for item in items if item.get('source') == 'SEC']
    finra = [item for item in items if item.get('source') == 'FINRA']
    cftc = [item for item in items if item.get('source') == 'CFTC']
    comments = [item for item in items if item.get('comment_period') == '1']
    safe_signup = html_text(signup_url)
    safe_read_more = html_text(read_more_url)

    def _stat(value: int, label: str) -> str:
        return (
            f'<td style="padding:12px 8px;text-align:center;">'
            f'<div style="font-size:28px;font-weight:700;color:#0b1f3a;line-height:1;">{value}</div>'
            f'<div style="margin-top:6px;font-size:12px;color:#64748b;">{html_text(label)}</div>'
            f'</td>'
        )

    def _item_html(item: dict[str, str]) -> str:
        title = html_text(item.get('title') or 'Untitled')
        kind = html_text(item.get('kind') or 'Agency action')
        source = html_text(item.get('source') or '')
        published = html_text(item.get('published_label') or '')
        url = html_text(item.get('url') or '#')
        meta = ' · '.join(part for part in (source, kind, published) if part)
        summary_html = build_summary_html(item.get('summary') or '')
        return f"""
          <tr>
            <td style="padding:0 0 18px;">
              <table role="presentation" width="100%" cellspacing="0" cellpadding="0"
                style="border:1px solid #e2e8f0;border-radius:12px;background:#ffffff;">
                <tr>
                  <td style="padding:18px 20px;">
                    <p style="margin:0 0 6px;font-size:11px;letter-spacing:0.08em;text-transform:uppercase;color:#64748b;font-weight:600;">{meta}</p>
                    <h3 style="margin:0 0 10px;font-size:17px;line-height:1.35;color:#0b1f3a;">{title}</h3>
                    {summary_html}
                    <p style="margin:0;">
                      <a href="{url}" style="font-size:13px;font-weight:600;color:#1d4f91;text-decoration:none;">Open publication</a>
                      <span style="margin-left:10px;font-size:11px;color:#94a3b8;">Summary by Reg-AI</span>
                    </p>
                  </td>
                </tr>
              </table>
            </td>
          </tr>
        """

    def _section(title: str, subtitle: str, rows: list[dict[str, str]]) -> str:
        if not rows:
            return ''
        cards = ''.join(_item_html(item) for item in rows)
        return f"""
          <tr>
            <td style="padding:8px 0 6px;">
              <h2 style="margin:0;font-size:13px;letter-spacing:0.14em;text-transform:uppercase;color:#0b1f3a;">{html_text(title)}</h2>
              <p style="margin:4px 0 12px;font-size:13px;color:#64748b;">{html_text(subtitle)}</p>
            </td>
          </tr>
          {cards}
        """

    comment_html = ''
    if comments:
        bullets = ''.join(
            f'<li style="margin:0 0 8px;">{html_text(item.get("title") or "")}</li>'
            for item in comments
        )
        comment_html = f"""
          <tr>
            <td style="padding:8px 0 18px;">
              <h2 style="margin:0 0 10px;font-size:13px;letter-spacing:0.14em;text-transform:uppercase;color:#0b1f3a;">Comment periods closing soon</h2>
              <ul style="margin:0;padding-left:1.15rem;color:#334155;font-size:14px;line-height:1.5;">{bullets}</ul>
            </td>
          </tr>
        """

    empty_state = ''
    if not items:
        empty_state = """
          <tr>
            <td style="padding:8px 0 18px;font-size:14px;color:#334155;">
              No SEC, FINRA, or CFTC publications were ingested for this week. The next brief will
              cover any new releases as soon as they appear in the Regulatory Feed.
            </td>
          </tr>
        """

    return f"""\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Weekly Regulatory Brief</title>
</head>
<body style="margin:0;padding:0;background:#eef2f7;color:#0f172a;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">
  <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#eef2f7;padding:28px 12px;">
    <tr>
      <td align="center">
        <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:640px;background:#f8fafc;border-radius:16px;overflow:hidden;box-shadow:0 10px 30px rgba(15,23,42,0.08);">
          <tr>
            <td style="background:linear-gradient(135deg,#0b1f3a 0%,#12315a 55%,#1d4f91 100%);padding:28px 28px 22px;">
              <p style="margin:0;font-size:12px;letter-spacing:0.18em;text-transform:uppercase;color:#93c5fd;font-weight:600;">Reg AI</p>
              <h1 style="margin:8px 0 0;font-size:26px;line-height:1.2;color:#ffffff;font-weight:700;">Weekly Regulatory Brief</h1>
              <p style="margin:8px 0 0;font-size:14px;color:#dbeafe;">Week ending {html_text(ending_label)}</p>
              <p style="margin:10px 0 0;font-size:13px;line-height:1.5;color:#bfdbfe;">
                Every SEC, FINRA, and CFTC publication from the past seven days, summarized from the
                Reg-AI Regulatory Feed.
              </p>
            </td>
          </tr>
          <tr>
            <td style="padding:18px 20px 8px;background:#ffffff;">
              <table role="presentation" width="100%" cellspacing="0" cellpadding="0">
                <tr>
                  {_stat(len(sec), 'SEC releases')}
                  {_stat(len(finra), 'FINRA items')}
                  {_stat(len(cftc), 'CFTC items')}
                  {_stat(len(comments), 'open comment periods')}
                </tr>
              </table>
            </td>
          </tr>
          <tr>
            <td style="padding:8px 20px 12px;">
              <table role="presentation" width="100%" cellspacing="0" cellpadding="0">
                {_section('SEC · Securities and Exchange Commission', 'Commission releases from the Regulatory Feed', sec)}
                {_section('FINRA · Financial Industry Regulatory Authority', 'Notices and filings from the Regulatory Feed', finra)}
                {_section('CFTC · Commodity Futures Trading Commission', 'Press room items from the Regulatory Feed', cftc)}
                <tr>
                  <td align="center" style="padding:10px 0 20px;text-align:center;">
                    <a href="{safe_read_more}" style="display:inline-block;font-size:15px;font-weight:700;color:#1d4f91;text-decoration:none;">
                      Read More
                    </a>
                  </td>
                </tr>
                {comment_html}
                {empty_state}
              </table>
            </td>
          </tr>
          <tr>
            <td style="padding:8px 20px 24px;">
              <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#0b1f3a;border-radius:12px;">
                <tr>
                  <td style="padding:22px 22px 20px;">
                    <p style="margin:0 0 8px;font-size:16px;font-weight:700;color:#ffffff;">
                      Turn this briefing into policy action
                    </p>
                    <p style="margin:0 0 16px;font-size:14px;line-height:1.55;color:#dbeafe;">
                      Open a Reg-AI organization to run AI policy scans and redlines against the same
                      SEC, FINRA, and CFTC publications in this newsletter.
                    </p>
                    <a href="{safe_signup}" style="display:inline-block;background:#ffffff;color:#0b1f3a;padding:12px 18px;border-radius:8px;font-weight:700;font-size:14px;text-decoration:none;">
                      Create a Reg-AI account
                    </a>
                  </td>
                </tr>
              </table>
            </td>
          </tr>
          <tr>
            <td style="padding:0 20px 24px;">
              <p style="margin:0;font-size:12px;line-height:1.55;color:#94a3b8;">
                Summaries are generated by Reg-AI and are provided for informational purposes only.
                They are not legal advice and do not constitute compliance clearance. Always refer to
                the original publication linked above.
              </p>
              <p style="margin:10px 0 0;font-size:12px;color:#94a3b8;">reg-ai.ipxenterprises.com</p>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>
"""
