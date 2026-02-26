"""
Wagtail admin view for building and publishing static site to S3.

The S3 bucket is read from the BAKERY_AWS_BUCKET_NAME environment variable
(or AWS_BUCKET_NAME as fallback). AWS credentials use the standard
AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY environment variables.
"""

import io
import json
import logging
import os

from django.contrib import messages
from django.core.management import call_command
from django.http import HttpResponseRedirect, StreamingHttpResponse
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


def _get_post_publish_command(settings):
    """
    Parse BAKERY_POST_PUBLISH_COMMAND. Returns (command, title) or (None, None).

    Accepts:
    - dict: {"command": "name", "title": "Label"}
    - str: command name (title defaults to "Post-publish")
    - None: no post-publish step
    """
    val = getattr(settings, "BAKERY_POST_PUBLISH_COMMAND", None)
    if val is None:
        return None, None
    if isinstance(val, dict):
        return val.get("command"), val.get("title", "Post-publish")
    if isinstance(val, str):
        return val, "Post-publish"
    return None, None


def _sse_event(data):
    """Format data as Server-Sent Event."""
    return f"data: {json.dumps(data)}\n\n"


def _run_bakery_stream(action, bucket_name):
    """Generator that yields SSE events for build/sync/invalidate steps."""
    from django.conf import settings

    build_kwargs = {}
    if getattr(settings, "BAKERY_SKIP_STATIC", False):
        build_kwargs["skip_static"] = True

    try:
        if action in ("build", "build_publish"):
            yield _sse_event({"step": "build", "status": "running", "label": "Build"})
            out, err = io.StringIO(), io.StringIO()
            call_command("build", stdout=out, stderr=err, **build_kwargs)
            logger.info("Bakery build completed: %s", out.getvalue()[:500])
            yield _sse_event({"step": "build", "status": "complete", "label": "Build"})

        if action == "build_publish":
            if not bucket_name:
                yield _sse_event({"step": "error", "message": "S3 bucket not configured"})
                return

            yield _sse_event({"step": "sync", "status": "running", "label": "Sync to S3"})
            out, err = io.StringIO(), io.StringIO()
            call_command("publish", aws_bucket_name=bucket_name, stdout=out, stderr=err)
            logger.info("Bakery publish completed: %s", out.getvalue()[:500])
            yield _sse_event({"step": "sync", "status": "complete", "label": "Sync to S3"})

            post_cmd, post_cmd_title = _get_post_publish_command(settings)
            if post_cmd:
                yield _sse_event({"step": "post_publish", "status": "running", "label": post_cmd_title})
                try:
                    out, err = io.StringIO(), io.StringIO()
                    call_command(post_cmd, stdout=out, stderr=err)
                    logger.info("Post-publish command %s completed", post_cmd)
                    yield _sse_event({"step": "post_publish", "status": "complete", "label": post_cmd_title})
                except Exception as e:
                    logger.exception("Post-publish command %s failed", post_cmd)
                    yield _sse_event({"step": "post_publish", "status": "error", "label": post_cmd_title, "message": str(e)})
                    yield _sse_event({"step": "done", "success": False, "message": f"Post-publish failed: {e}"})
                    return

        yield _sse_event({"step": "done", "success": True})
    except Exception as e:
        logger.exception("Bakery %s failed", action)
        yield _sse_event({"step": "done", "success": False, "message": str(e)})


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

        # Stream progress when requested (fetch with Accept: text/event-stream)
        wants_stream = "text/event-stream" in request.headers.get("Accept", "")
        if wants_stream:
            response = StreamingHttpResponse(
                _run_bakery_stream(action, bucket_name),
                content_type="text/event-stream",
            )
            response["Cache-Control"] = "no-cache"
            response["X-Accel-Buffering"] = "no"
            return response

        out = io.StringIO()
        err = io.StringIO()

        try:
            if action in ("build", "build_publish"):
                build_kwargs = {}
                if getattr(settings, "BAKERY_SKIP_STATIC", False):
                    build_kwargs["skip_static"] = True
                call_command("build", stdout=out, stderr=err, **build_kwargs)
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

                post_cmd, _ = _get_post_publish_command(settings)
                if post_cmd:
                    try:
                        out = io.StringIO()
                        err = io.StringIO()
                        call_command(post_cmd, stdout=out, stderr=err)
                        messages.success(
                            request,
                            "Build, publish to S3, and frontend cache purge completed.",
                        )
                        logger.info("Post-publish command %s completed", post_cmd)
                    except Exception as purge_err:
                        logger.exception("Post-publish command %s failed", post_cmd)
                        messages.warning(
                            request,
                            "Build and publish succeeded, but purge failed: %s",
                            purge_err,
                        )

        except Exception as e:
            logger.exception("Bakery %s failed", action)
            messages.error(
                request,
                f"Build failed: {e}",
            )

        return HttpResponseRedirect(reverse("wagtailbakery_admin"))

    post_cmd, post_cmd_title = _get_post_publish_command(settings)

    context = {
        "bucket_name": bucket_name or "(not set)",
        "bucket_configured": bool(bucket_name),
        "build_dir": build_dir,
        "post_publish_command": post_cmd,
        "post_publish_command_title": post_cmd_title or "Post-publish",
    }
    return render(request, "wagtailbakery/admin.html", context)
