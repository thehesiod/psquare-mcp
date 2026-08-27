"""Pins the attachment-href predicate against substring spoofing.

These hrefs come out of school-authored post and message HTML, and ``get_post``
hands the resulting ``Attachment.url`` straight to ``_fetch_image`` /
``_fetch_pdf_text``, which fetch it with the caller's authenticated session. The
predicate used to be ``"s3.amazonaws.com" in href``, so any URL that merely
mentioned the string anywhere — query string included — was fetched.

``redact_url`` covers the other half: pre-signed URLs carry their credentials in
the query string, and both fetch paths log the URL on failure.
"""

import pytest

from parentsquare_mcp.urls import is_attachment_href, redact_url


@pytest.mark.parametrize(
    "href",
    [
        "https://s3.amazonaws.com/psq-bucket/flyer.pdf",
        "https://psq-bucket.s3.amazonaws.com/flyer.pdf",
        "https://psq-bucket.s3.us-west-2.amazonaws.com/flyer.pdf",
        "https://s3-us-west-2.amazonaws.com/psq-bucket/flyer.pdf",
    ],
)
def test_s3_hosts_are_attachments(href):
    assert is_attachment_href(href)


def test_cloudfront_signed_download_is_an_attachment():
    # CloudFront fronts the same objects and keeps S3's disposition param.
    href = (
        "https://d1abc.cloudfront.net/uploads/flyer.pdf"
        "?response-content-disposition=attachment%3B%20filename%3D%22Spring%20Flyer.pdf%22"
        "&Signature=abc123&Key-Pair-Id=APKA&Expires=1790000000"
    )
    assert is_attachment_href(href)


@pytest.mark.parametrize(
    "href",
    [
        "https://evil.example/?x=s3.amazonaws.com",
        "https://evil.example/s3.amazonaws.com/flyer.pdf",
        "https://s3.amazonaws.com.evil.example/flyer.pdf",
        # Disposition alone is bait; a real pre-signed URL is also signed.
        "https://evil.example/?response-content-disposition=attachment",
        "",
    ],
)
def test_spoofed_hrefs_are_not_attachments(href):
    assert not is_attachment_href(href)


def test_redact_url_drops_presigned_credentials():
    url = (
        "https://psq-bucket.s3.amazonaws.com/uploads/flyer.pdf"
        "?X-Amz-Credential=AKIA%2F20260826%2Fus-west-2%2Fs3%2Faws4_request"
        "&X-Amz-Signature=deadbeef"
    )
    assert redact_url(url) == "https://psq-bucket.s3.amazonaws.com/uploads/flyer.pdf"


def test_redact_url_drops_userinfo_and_keeps_port():
    assert redact_url("https://user:pw@host.example:8443/a/b?t=1") == "https://host.example:8443/a/b"
