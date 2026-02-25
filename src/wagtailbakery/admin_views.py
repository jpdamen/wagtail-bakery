"""
Wagtail admin view for building and publishing static site to S3.

The S3 bucket is read from the BAKERY_AWS_BUCKET_NAME environment variable
(or AWS_BUCKET_NAME as fallback). AWS credentials use the standard
AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY environment variables.
"""

import io
import logging
import os

from django.contrib import messages
from django.core.management import call_command
from django.http import HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.http import require_http_methods
from django.views.decorators.vary import vary_on_headers

from wagtail.admin.auth import require_admin_access

logger = logging.getLogger(__name__)


def get_bucket_name():
    """Get S3 bucket name from environment or settings."""
    return os.environ.get(
        "BAKERY_AWS_BUCKET_NAME",
        os.environ.get("AWS_BUCKET_NAME", ""),
    )


@require_admin_access
@require_http_methods(["GET", "POST"])
@vary_on_headers("X-Requested-With")
def bakery_admin_view(request):
    """
    Admin view to build and optionally publish the static site to S3.
    """
    from django.conf import settings

    bucket_name = get_bucket_name()
    build_dir = getattr(settings, "BUILD_DIR", "")

    if request.method == "POST":
        action = request.POST.get("action")
        if action not in ("build", "build_publish"):
            messages.error(request, "Invalid action.")
            return HttpResponseRedirect(reverse("wagtailbakery_admin"))

        out = io.StringIO()
        err = io.StringIO()

        try:
            if action in ("build", "build_publish"):
                call_command("build", stdout=out, stderr=err)
                build_output = out.getvalue()
                if err.getvalue():
                    build_output += "\n" + err.getvalue()
                messages.success(
                    request,
                    "Build completed successfully.",
                )
                logger.info("Bakery build completed: %s", build_output[:500])

            if action == "build_publish":
                if not bucket_name:
                    messages.error(
                        request,
                        "S3 bucket not configured. Set BAKERY_AWS_BUCKET_NAME "
                        "or AWS_BUCKET_NAME in the environment.",
                    )
                    return HttpResponseRedirect(reverse("wagtailbakery_admin"))

                out = io.StringIO()
                err = io.StringIO()
                call_command(
                    "publish",
                    aws_bucket_name=bucket_name,
                    stdout=out,
                    stderr=err,
                )
                publish_output = out.getvalue()
                if err.getvalue():
                    publish_output += "\n" + err.getvalue()
                messages.success(
                    request,
                    "Build and publish to S3 completed successfully.",
                )
                logger.info(
                    "Bakery publish completed: %s",
                    publish_output[:500],
                )

        except Exception as e:
            logger.exception("Bakery %s failed", action)
            messages.error(
                request,
                f"Build failed: {e}",
            )

        return HttpResponseRedirect(reverse("wagtailbakery_admin"))

    context = {
        "bucket_name": bucket_name or "(not set)",
        "bucket_configured": bool(bucket_name),
        "build_dir": build_dir,
    }
    return render(request, "wagtailbakery/admin.html", context)
