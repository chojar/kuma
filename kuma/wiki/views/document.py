import newrelic.agent
from django.conf import settings
from django.contrib import messages
from django.http import (
    Http404,
    HttpResponse,
    HttpResponseBadRequest,
    HttpResponsePermanentRedirect,
    JsonResponse,
)
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.cache import add_never_cache_headers, patch_vary_headers
from django.utils.safestring import mark_safe
from django.utils.translation import gettext
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_http_methods, require_POST
from ratelimit.decorators import ratelimit

import kuma.wiki.content
from kuma.core.decorators import (
    block_user_agents,
    ensure_wiki_domain,
    login_required,
    permission_required,
    shared_cache_control,
    superuser_required,
)
from kuma.core.urlresolvers import reverse
from kuma.core.utils import is_wiki, redirect_to_wiki, urlparams
from kuma.wiki.templatetags.jinja_helpers import absolutify

from .utils import split_slug
from .. import kumascript
from ..constants import SLUG_CLEANSING_RE, WIKI_ONLY_DOCUMENT_QUERY_PARAMS
from ..decorators import (
    allow_CORS_GET,
    check_readonly,
    prevent_indexing,
    process_document_path,
)
from ..events import EditDocumentEvent, EditDocumentInTreeEvent
from ..forms import TreeMoveForm
from ..models import Document, DocumentDeletionLog, DocumentRenderedContentNotAvailable
from ..tasks import move_page


def _get_html_and_errors(request, doc, rendering_params):
    """
    Get HTML and rendering errors for a Document.

    Return is a tuple:
    * The HTML
    * A list of KumaScript errors encountered during rendering
    * True if rendered content was requested but not available

    If rendering_params['use_rendered'] is True, then KumaScript rendering is
    attempted. If False, pre-rendered content is returned, if any.
    """
    doc_html, ks_errors, render_raw_fallback = doc.html, None, False
    if not rendering_params["use_rendered"]:
        return doc_html, ks_errors, render_raw_fallback

    # A logged-in user can schedule a full re-render with Shift-Reload
    cache_control = None
    if request.user.is_authenticated:
        # Shift-Reload sends Cache-Control: no-cache
        ua_cc = request.META.get("HTTP_CACHE_CONTROL")
        if ua_cc == "no-cache":
            cache_control = "no-cache"

    base_url = request.build_absolute_uri("/")
    try:
        r_body, r_errors = doc.get_rendered(cache_control, base_url)
        if r_body:
            doc_html = r_body
        if r_errors:
            ks_errors = r_errors
    except DocumentRenderedContentNotAvailable:
        # There was no rendered content available, and we were unable
        # to render it on the spot. So, fall back to presenting raw
        # content
        render_raw_fallback = True

    return doc_html, ks_errors, render_raw_fallback


def _make_doc_structure(document, level, expand, depth):
    if document.is_redirect:
        return None

    if expand:
        result = dict(document.get_json_data())
        result["subpages"] = []
    else:
        result = {
            "title": document.title,
            "slug": document.slug,
            "locale": document.locale,
            "url": document.get_absolute_url(),
            "subpages": [],
        }

    if level < depth:
        descendants = document.get_descendants(1)
        descendants.sort(key=lambda item: item.title)
        for descendant in descendants:
            subpage = _make_doc_structure(descendant, level + 1, expand, depth)
            if subpage is not None:
                result["subpages"].append(subpage)
    return result


def _get_seo_parent_title(document, slug_dict, document_locale):
    """
    Get parent-title information for SEO purposes.
    """
    seo_doc_slug = slug_dict["seo_root"]
    seo_root_doc = None

    if seo_doc_slug:
        # If the SEO root doc is the parent topic, save a query
        if document.parent_topic_id and document.parent_topic.slug == seo_doc_slug:
            seo_root_doc = document.parent_topic
        else:
            try:
                seo_root_doc = Document.objects.only("title").get(
                    locale=document_locale, slug=seo_doc_slug
                )
            except Document.DoesNotExist:
                pass

    if seo_root_doc:
        return " - {}".format(seo_root_doc.title)
    else:
        return ""


def _filter_doc_html(request, doc, doc_html, rendering_params):
    """
    Apply needed filtering/annotating operations to a Document's HTML.
    """
    # If ?summary is on, just serve up the summary as doc HTML
    if rendering_params["summary"]:
        return doc.get_summary_html()

    # Shortcut the parsing & filtering, if none of these relevant rendering
    # params are set.
    if not (
        rendering_params["section"]
        or rendering_params["raw"]
        or rendering_params["edit_links"]
        or rendering_params["include"]
    ):
        return doc_html

    # TODO: One more view-time content parsing instance to refactor
    tool = kuma.wiki.content.parse(doc_html)

    # ?raw view is often used for editors - apply safety filtering.
    # TODO: Should this stuff happen in render() itself?
    if rendering_params["raw"]:
        # TODO: There will be no need to call "injectSectionIDs" or
        #       "filterEditorSafety" when the code that calls "clean_content"
        #       on Revision.save is deployed to production, AND the current
        #       revisions of all docs have had their content cleaned with
        #       "clean_content".
        tool.injectSectionIDs()
        tool.filterEditorSafety()

    # If a section ID is specified, extract that section.
    # TODO: Pre-extract every section on render? Might be over-optimization
    if rendering_params["section"]:
        tool.extractSection(rendering_params["section"])

    # If this user can edit the document, inject section editing links.
    # TODO: Rework so that this happens on the client side?
    if (
        rendering_params["edit_links"] or not rendering_params["raw"]
    ) and request.user.is_authenticated:
        tool.injectSectionEditingLinks(doc.slug, doc.locale)

    doc_html = tool.serialize()

    # If this is an include, filter out the class="noinclude" blocks.
    # TODO: Any way to make this work in rendering? Possibly over-optimization,
    # because this is often paired with ?section - so we'd need to store every
    # section twice for with & without include sections
    if rendering_params["include"]:
        doc_html = kuma.wiki.content.filter_out_noinclude(doc_html)

    return doc_html


def _add_kuma_revision_header(doc, response):
    """
    Add the X-kuma-revision header to the response if applicable.
    """
    if doc.current_revision_id:
        response["X-kuma-revision"] = doc.current_revision_id
    return response


def _default_locale_fallback(request, document_slug, document_locale):
    """
    If we're falling back to a Document in the default locale, figure
    out why and whether we can redirect to a translation in the
    requested locale.

    """
    fallback_doc = None
    redirect_url = None
    fallback_reason = None

    try:
        fallback_doc = Document.objects.get(
            slug=document_slug, locale=settings.WIKI_DEFAULT_LANGUAGE
        )

        # If there's a translation to the requested locale, take it:
        translation = fallback_doc.translated_to(document_locale)

        if translation and translation.current_revision:
            url = translation.get_absolute_url()
            redirect_url = urlparams(url, query_dict=request.GET)
        elif translation and fallback_doc.current_revision:
            # Found a translation but its current_revision is None
            # and OK to fall back to parent (parent is approved).
            fallback_reason = "translation_not_approved"
        elif fallback_doc.current_revision:
            # There is no translation
            # and OK to fall back to parent (parent is approved).
            fallback_reason = "no_translation"
    except Document.DoesNotExist:
        pass

    return fallback_doc, fallback_reason, redirect_url


def _get_deleted_parent_redirect_url(document_locale, document_slug):
    for document in Document.deleted_objects.filter(
        locale=document_locale, slug=document_slug, parent__isnull=False
    ):
        if document.parent:
            return document.parent.get_absolute_url()


def _get_doc_and_fallback_reason(document_locale, document_slug):
    """
    Attempt to fetch a Document at the given locale and slug, and
    return it, or return a fallback reason if we weren't able to.

    """
    doc = None
    fallback_reason = None

    # Optimizing the queryset to fetch the required values only
    related_fields = [
        "current_revision",
        "current_revision__creator",
        "parent",
        "parent__current_revision",
        "parent_topic",
    ]
    current_revision_fields = [
        "current_revision__{}".format(field)
        for field in (
            "toc_depth",
            "created",
            "creator__id",
            "creator__username",
            "creator__is_active",
        )
    ]
    parent_fields = [
        "parent__{}".format(field)
        for field in ("locale", "slug", "current_revision__slug")
    ]
    parent_topic_fields = [
        "parent_topic__{}".format(field) for field in ("id", "title", "slug")
    ]

    document_fields = [
        "html",
        "rendered_html",
        "body_html",
        "locale",
        "slug",
        "title",
        "is_localizable",
        "rendered_errors",
        "toc_html",
        "summary_html",
        "summary_text",
        "quick_links_html",
    ]

    fields = (
        document_fields + current_revision_fields + parent_fields + parent_topic_fields
    )

    try:
        doc = (
            Document.objects.only(*fields)
            .select_related(*related_fields)
            .get(locale=document_locale, slug=document_slug)
        )

        if not doc.current_revision_id and doc.parent and doc.parent.current_revision:
            # This is a translation but its current_revision is None
            # and OK to fall back to parent (parent is approved).
            fallback_reason = "translation_not_approved"
        elif not doc.current_revision_id:
            fallback_reason = "no_content"
    except Document.DoesNotExist:
        pass

    return doc, fallback_reason


def _apply_content_experiment(request, doc):
    """
    Get Document and rendering parameters changed by the content experiment.

    If the page is under a content experiment and the selected variant is
    valid, the return is (the variant Document, the experiment params).

    If the page is under a content experiment but the variant is invalid or
    not selected, the return is (original Document, the experiment params).

    If the page is not under a content experiment, the return is
    (original Document, None).
    """
    key = "%s:%s" % (doc.locale, doc.slug)
    for experiment in settings.CONTENT_EXPERIMENTS:
        if key in experiment["pages"]:
            # This page is under a content experiment
            variants = experiment["pages"][key]
            exp_params = {
                "id": experiment["id"],
                "ga_name": experiment["ga_name"],
                "param": experiment["param"],
                "original_path": request.path,
                "variants": variants,
                "selected": None,
                "selection_is_valid": None,
            }

            # Which variant was selected?
            selected = request.GET.get(experiment["param"])
            if selected:
                exp_params["selection_is_valid"] = False
                for variant, variant_slug in variants.items():
                    if selected == variant:
                        try:
                            content_doc = Document.objects.get(
                                locale=doc.locale, slug=variant_slug
                            )
                        except Document.DoesNotExist:
                            pass
                        else:
                            # Valid variant selected
                            exp_params["selected"] = selected
                            exp_params["selection_is_valid"] = True
                            return content_doc, exp_params
            return doc, exp_params  # No (valid) variant selected
    return doc, None  # Not a content experiment


@shared_cache_control
@block_user_agents
@require_GET
@allow_CORS_GET
@process_document_path
def children(request, document_slug, document_locale):
    """
    Retrieves a document and returns its children in a JSON structure
    """
    expand = "expand" in request.GET
    max_depth = 5
    depth = int(request.GET.get("depth", max_depth))
    if depth > max_depth:
        depth = max_depth

    result = []
    try:
        doc = Document.objects.get(locale=document_locale, slug=document_slug)
        result = _make_doc_structure(doc, 0, expand, depth)
        if result is None:
            result = {"error": "Document has moved."}
    except Document.DoesNotExist:
        result = {"error": "Document does not exist."}

    return JsonResponse(result)


@ensure_wiki_domain
@never_cache
@block_user_agents
@require_http_methods(["GET", "POST"])
@permission_required("wiki.move_tree")
@process_document_path
@check_readonly
@prevent_indexing
def move(request, document_slug, document_locale):
    """
    Move a tree of pages
    """
    doc = get_object_or_404(Document, locale=document_locale, slug=document_slug)

    descendants = doc.get_descendants()
    slug_split = split_slug(doc.slug)

    if request.method == "POST":
        form = TreeMoveForm(initial=request.GET, data=request.POST)
        if form.is_valid():
            conflicts = doc._tree_conflicts(form.cleaned_data["slug"])
            if conflicts:
                return render(
                    request,
                    "wiki/move.html",
                    {
                        "form": form,
                        "document": doc,
                        "descendants": descendants,
                        "descendants_count": len(descendants),
                        "conflicts": conflicts,
                        "SLUG_CLEANSING_RE": SLUG_CLEANSING_RE,
                    },
                )
            move_page.delay(
                document_locale,
                document_slug,
                form.cleaned_data["slug"],
                request.user.id,
            )
            return render(
                request, "wiki/move_requested.html", {"form": form, "document": doc}
            )
    else:
        form = TreeMoveForm()

    return render(
        request,
        "wiki/move.html",
        {
            "form": form,
            "document": doc,
            "descendants": descendants,
            "descendants_count": len(descendants),
            "SLUG_CLEANSING_RE": SLUG_CLEANSING_RE,
            "specific_slug": slug_split["specific"],
        },
    )


@ensure_wiki_domain
@never_cache
@block_user_agents
@process_document_path
@superuser_required
@check_readonly
def repair_breadcrumbs(request, document_slug, document_locale):
    doc = get_object_or_404(Document, locale=document_locale, slug=document_slug)
    doc.repair_breadcrumbs()
    return redirect(doc.get_absolute_url())


@ensure_wiki_domain
@shared_cache_control
@require_GET
@allow_CORS_GET
@process_document_path
@prevent_indexing
@ratelimit(key="user_or_ip", rate="400/m", block=True)
def toc(request, document_slug=None, document_locale=None):
    """
    Return a document's table of contents as HTML.
    """
    query = {
        "locale": request.LANGUAGE_CODE,
        "current_revision__isnull": False,
    }
    if document_slug is not None:
        query["slug"] = document_slug
        query["locale"] = document_locale
    elif "title" in request.GET:
        query["title"] = request.GET["title"]
    elif "slug" in request.GET:
        query["slug"] = request.GET["slug"]
    else:
        return HttpResponseBadRequest()

    document = get_object_or_404(Document, **query)
    toc_html = document.get_toc_html()
    if toc_html:
        toc_html = "<ol>" + toc_html + "</ol>"

    return HttpResponse(toc_html)


@shared_cache_control
@block_user_agents
@require_GET
@allow_CORS_GET
@process_document_path
@prevent_indexing
def as_json(request, document_slug=None, document_locale=None):
    """
    Return some basic document info in a JSON blob.
    """
    kwargs = {
        "locale": request.LANGUAGE_CODE,
        "current_revision__isnull": False,
    }
    if document_slug is not None:
        kwargs["slug"] = document_slug
        kwargs["locale"] = document_locale
    elif "title" in request.GET:
        kwargs["title"] = request.GET["title"]
    elif "slug" in request.GET:
        kwargs["slug"] = request.GET["slug"]
    else:
        return HttpResponseBadRequest()

    document = get_object_or_404(Document, **kwargs)
    # TODO: There will be no need for the following line of code when the
    #       code that calls "clean_content" on Revision.save is deployed to
    #       production, AND the current revisions of all docs have had their
    #       content cleaned with "clean_content".
    (kuma.wiki.content.parse(document.html).injectSectionIDs().serialize())

    stale = True
    if is_wiki(request) and request.user.is_authenticated:
        # From the Wiki domain, a logged-in user can demand fresh data with
        # a shift-reload (which sends "Cache-Control: no-cache").
        ua_cc = request.META.get("HTTP_CACHE_CONTROL")
        if ua_cc == "no-cache":
            stale = False

    data = document.get_json_data(stale=stale)
    return JsonResponse(data)


@ensure_wiki_domain
@never_cache
@csrf_exempt
@block_user_agents
@require_POST
@login_required
@process_document_path
def subscribe(request, document_slug, document_locale):
    """
    Toggle watching a document for edits.
    """
    document = get_object_or_404(Document, locale=document_locale, slug=document_slug)
    status = 0

    if EditDocumentEvent.is_notifying(request.user, document):
        EditDocumentEvent.stop_notifying(request.user, document)
    else:
        EditDocumentEvent.notify(request.user, document)
        status = 1

    if request.is_ajax():
        return JsonResponse({"status": status})
    else:
        return redirect(document)


@ensure_wiki_domain
@never_cache
@csrf_exempt
@block_user_agents
@require_POST
@login_required
@process_document_path
def subscribe_to_tree(request, document_slug, document_locale):
    """
    Toggle watching a tree of documents for edits.
    """
    document = get_object_or_404(Document, locale=document_locale, slug=document_slug)
    status = 0

    if EditDocumentInTreeEvent.is_notifying(request.user, document):
        EditDocumentInTreeEvent.stop_notifying(request.user, document)
    else:
        EditDocumentInTreeEvent.notify(request.user, document)
        status = 1

    if request.is_ajax():
        return JsonResponse({"status": status})
    else:
        return redirect(document)


def _document_redirect_to_create(document_slug, document_locale, slug_dict):
    """
    When a Document doesn't exist but the user can create it, return
    the creation URL to redirect to.
    """
    url = reverse("wiki.create", locale=document_locale)
    if slug_dict["length"] > 1:
        parent_doc = get_object_or_404(
            Document, locale=document_locale, slug=slug_dict["parent"]
        )
        if parent_doc.is_redirect:
            parent_doc = parent_doc.get_redirect_document(id_only=True)
            if parent_doc is None:
                # Redirect is not to a Document, can't create subpage
                raise Http404()

        url = urlparams(url, parent=parent_doc.id, slug=slug_dict["specific"])
    else:
        # This is a "base level" redirect, i.e. no parent
        url = urlparams(url, slug=document_slug)
    return url


@newrelic.agent.function_trace()
@prevent_indexing
def _document_deleted(request, deletion_logs):
    """
    When a Document has been deleted return a 404.

    If the user can restore documents, then return a 404 but also include the
    template with the form to restore the document.
    """
    if request.user and request.user.has_perm("wiki.restore_document"):
        deletion_log = deletion_logs.order_by("-pk")[0]
        context = {"deletion_log": deletion_log}
        response = render(request, "wiki/deletion_log.html", context, status=404)
        add_never_cache_headers(response)
        return response

    raise Http404


@newrelic.agent.function_trace()
def _document_raw(doc_html):
    """
    Display a raw Document.
    """
    response = HttpResponse(doc_html)
    response["X-Frame-Options"] = "Allow"
    response["X-Robots-Tag"] = "noindex"
    return response


@shared_cache_control
@csrf_exempt
@require_http_methods(["GET", "HEAD"])
@allow_CORS_GET
@process_document_path
@newrelic.agent.function_trace()
@ratelimit(key="user_or_ip", rate="1200/m", block=True)
def document(request, document_slug, document_locale):
    if is_wiki(request):
        return wiki_document(request, document_slug, document_locale)

    return react_document(request, document_slug, document_locale)


def wiki_document(request, document_slug, document_locale):
    """
    View a wiki document.
    """
    slug_dict = split_slug(document_slug)

    # Is there a document at this slug, in this locale?
    doc, fallback_reason = _get_doc_and_fallback_reason(document_locale, document_slug)

    if doc is None:
        # Possible the document once existed, but is now deleted.
        # If so, show that it was deleted.
        deletion_log_entries = DocumentDeletionLog.objects.filter(
            locale=document_locale, slug=document_slug
        )
        if deletion_log_entries.exists():
            # Show deletion log and restore / purge for soft-deleted docs
            deleted_doc = Document.deleted_objects.filter(
                locale=document_locale, slug=document_slug
            )
            if deleted_doc.exists():
                return _document_deleted(request, deletion_log_entries)

        # We can throw a 404 immediately if the request type is HEAD.
        # TODO: take a shortcut if the document was found?
        if request.method == "HEAD":
            raise Http404

        # Check if we should fall back to default locale.
        fallback_doc, fallback_reason, redirect_url = _default_locale_fallback(
            request, document_slug, document_locale
        )
        if fallback_doc is not None:
            doc = fallback_doc
            if redirect_url is not None:
                return redirect(redirect_url)
        else:
            # If a Document is not found, we may 404 immediately based on
            # request parameters.
            if (
                any(
                    [
                        request.GET.get(param, None)
                        for param in ("raw", "include", "nocreate")
                    ]
                )
                or not request.user.is_authenticated
            ):
                raise Http404

            # The user may be trying to create a child page; if a parent exists
            # for this document, redirect them to the "Create" page
            # Otherwise, they could be trying to create a main level doc.
            create_url = _document_redirect_to_create(
                document_slug, document_locale, slug_dict
            )
            response = redirect(create_url)
            add_never_cache_headers(response)
            return response

    # We found a Document. Now we need to figure out how we're going
    # to display it.

    # If we're a redirect, and redirecting hasn't been disabled, redirect.

    # Obey explicit redirect pages:
    # Don't redirect on redirect=no (like Wikipedia), so we can link from a
    # redirected-to-page back to a "Redirected from..." link, so you can edit
    # the redirect.
    redirect_url = (
        None if request.GET.get("redirect") == "no" else doc.get_redirect_url()
    )

    if redirect_url and redirect_url != doc.get_absolute_url():
        url = urlparams(redirect_url, query_dict=request.GET)
        # TODO: Re-enable the link in this message after Django >1.5 upgrade
        # Redirected from <a href="%(url)s?redirect=no">%(url)s</a>
        messages.add_message(
            request,
            messages.WARNING,
            mark_safe(
                gettext("Redirected from %(url)s")
                % {"url": request.build_absolute_uri(doc.get_absolute_url())}
            ),
            extra_tags="wiki_redirect",
        )
        return HttpResponsePermanentRedirect(url)

    # Read some request params to see what we're supposed to do.
    rendering_params = {}
    for param in ("raw", "summary", "include", "edit_links"):
        rendering_params[param] = request.GET.get(param, False) is not False
    rendering_params["section"] = request.GET.get("section", None)
    rendering_params["render_raw_fallback"] = False

    # Are we in a content experiment?
    original_doc = doc
    doc, exp_params = _apply_content_experiment(request, doc)
    rendering_params["experiment"] = exp_params

    # Get us some HTML to play with.
    rendering_params["use_rendered"] = kumascript.should_use_rendered(doc, request.GET)
    doc_html, ks_errors, render_raw_fallback = _get_html_and_errors(
        request, doc, rendering_params
    )
    rendering_params["render_raw_fallback"] = render_raw_fallback

    # Start parsing and applying filters.
    if doc.show_toc and not rendering_params["raw"]:
        toc_html = doc.get_toc_html()
    else:
        toc_html = None
    doc_html = _filter_doc_html(request, doc, doc_html, rendering_params)

    if rendering_params["raw"]:
        response = _document_raw(doc_html)
    else:
        # Get the SEO summary
        seo_summary = doc.get_summary_text()

        # Get the additional title information, if necessary.
        seo_parent_title = _get_seo_parent_title(
            original_doc, slug_dict, document_locale
        )

        # Retrieve pre-parsed content hunks
        quick_links_html = doc.get_quick_links_html()
        body_html = doc.get_body_html()

        # Record the English slug in Google Analytics,
        # to associate translations
        if original_doc.locale == "en-US":
            en_slug = original_doc.slug
        elif original_doc.parent_id and original_doc.parent.locale == "en-US":
            en_slug = original_doc.parent.slug
        else:
            en_slug = ""

        share_text = gettext("I learned about %(title)s on MDN.") % {"title": doc.title}

        contributors = doc.contributors
        contributors_count = len(contributors)
        has_contributors = contributors_count > 0
        other_translations = original_doc.get_other_translations(
            fields=["title", "locale", "slug", "parent"]
        )
        all_locales = {original_doc.locale} | set(
            trans.locale for trans in other_translations
        )

        # Bundle it all up and, finally, return.
        context = {
            "document": original_doc,
            "document_html": doc_html,
            "toc_html": toc_html,
            "quick_links_html": quick_links_html,
            "body_html": body_html,
            "contributors": contributors,
            "contributors_count": contributors_count,
            "contributors_limit": 6,
            "has_contributors": has_contributors,
            "fallback_reason": fallback_reason,
            "kumascript_errors": ks_errors,
            "macro_sources": (
                kumascript.macro_sources(force_lowercase_keys=True)
                if ks_errors
                else None
            ),
            "render_raw_fallback": rendering_params["render_raw_fallback"],
            "seo_summary": seo_summary,
            "seo_parent_title": seo_parent_title,
            "share_text": share_text,
            "analytics_page_revision": doc.current_revision_id,
            "analytics_en_slug": en_slug,
            "content_experiment": rendering_params["experiment"],
            "other_translations": other_translations,
            "all_locales": all_locales,
        }
        response = render(request, "wiki/document.html", context)

    if ks_errors or request.user.is_authenticated:
        add_never_cache_headers(response)

    # We're doing this to prevent any unknown intermediate public HTTP caches
    # from erroneously caching without considering cookies, since cookies do
    # affect the content of the response. The primary CDN is configured to
    # cache based on a whitelist of cookies.
    patch_vary_headers(response, ("Cookie",))

    return _add_kuma_revision_header(doc, response)


def react_document(request, document_slug, document_locale):
    """
    View a wiki document.
    """
    # If any query parameter is used that is only supported by the wiki view,
    # redirect to the wiki domain.
    if frozenset(request.GET) & WIKI_ONLY_DOCUMENT_QUERY_PARAMS:
        return redirect_to_wiki(request)

    slug_dict = split_slug(document_slug)

    # Is there a document at this slug, in this locale?
    doc, fallback_reason = _get_doc_and_fallback_reason(document_locale, document_slug)

    if doc is None:
        # We can throw a 404 immediately if the request type is HEAD.
        # TODO: take a shortcut if the document was found?
        if request.method == "HEAD":
            raise Http404

        # Check if we should fall back to default locale.
        fallback_doc, fallback_reason, redirect_url = _default_locale_fallback(
            request, document_slug, document_locale
        )
        if fallback_doc is not None:
            doc = fallback_doc
            if redirect_url is not None:
                return redirect(redirect_url)
        else:
            # It could be that the document you're trying to view was deleted and
            # the reason we're not finding a fallback is because the slug
            # doesn't match.
            # E.g. you're trying to view `/sv-SE/docs/Foö/Bår` but that document
            # was soft-deleted and its parent was `/en-US/docs/Foo/Bar`
            if document_locale != settings.LANGUAGE_CODE:  # Not in English!
                redirect_url = _get_deleted_parent_redirect_url(
                    document_locale, document_slug
                )
                if redirect_url:
                    return redirect(redirect_url)

            raise Http404

    # We found a Document. Now we need to figure out how we're going
    # to display it.

    # If we're a redirect, and redirecting hasn't been disabled, redirect.

    # Obey explicit redirect pages:
    # Don't redirect on redirect=no (like Wikipedia), so we can link from a
    # redirected-to-page back to a "Redirected from..." link, so you can edit
    # the redirect.
    redirect_url = (
        None if request.GET.get("redirect") == "no" else doc.get_redirect_url()
    )

    if redirect_url and redirect_url != doc.get_absolute_url():
        url = urlparams(redirect_url, query_dict=request.GET)
        # TODO: Re-enable the link in this message after Django >1.5 upgrade
        # Redirected from <a href="%(url)s?redirect=no">%(url)s</a>
        messages.add_message(
            request,
            messages.WARNING,
            mark_safe(
                gettext("Redirected from %(url)s")
                % {"url": request.build_absolute_uri(doc.get_absolute_url())}
            ),
            extra_tags="wiki_redirect",
        )
        return HttpResponsePermanentRedirect(url)

    # Get the SEO summary
    seo_summary = doc.get_summary_text()

    # Get the additional title information, if necessary.
    seo_parent_title = _get_seo_parent_title(doc, slug_dict, document_locale)

    # Get the JSON data for this document
    doc_api_data = document_api_data(doc)
    document_data = doc_api_data["documentData"]

    def robots_index():
        if fallback_reason:
            return False

        if not doc.html:
            return False

        if doc.is_experiment:
            return False

        if doc.has_legacy_namespace:
            return False

        if doc.has_noindex_slug:
            return False

        if request.get_host() not in settings.ALLOW_ROBOTS_WEB_DOMAINS:
            return False

        return True

    robots_meta_content = "index, follow" if robots_index() else "noindex, nofollow"

    # Bundle it all up and, finally, return.
    context = {
        "document_data": document_data,
        # TODO: anything we're actually using in the template ought
        # to be bundled up into the json object above instead.
        "seo_summary": seo_summary,
        "seo_parent_title": seo_parent_title,
        "robots_meta_content": robots_meta_content,
    }
    response = render(request, "wiki/react_document.html", context)

    return _add_kuma_revision_header(doc, response)


def document_api_data(doc=None, redirect_url=None):
    """
    Returns the JSON data for the document for the document API.
    """
    if redirect_url:
        return {
            "documentData": None,
            "redirectURL": redirect_url,
        }

    # The original english slug for this document, for google analytics
    if doc.locale == "en-US":
        en_slug = doc.slug
    elif doc.parent_id and doc.parent.locale == "en-US":
        en_slug = doc.parent.slug
    else:
        en_slug = ""

    other_translations = doc.get_other_translations(
        fields=("locale", "slug", "title", "parent")
    )
    available_locales = {doc.locale} | set(t.locale for t in other_translations)

    doc_absolute_url = doc.get_absolute_url()
    revision = doc.current_or_latest_revision()
    translation_status = None
    if doc.parent_id and revision and revision.localization_in_progress:
        translation_status = (
            "outdated" if revision.translation_age >= 10 else "in-progress"
        )
    return {
        "documentData": {
            "locale": doc.locale,
            "slug": doc.slug,
            "enSlug": en_slug,
            "id": doc.id,
            "title": doc.title,
            "summary": doc.get_summary_html(),
            "language": doc.language,
            "hrefLang": doc.get_hreflang(available_locales),
            "absoluteURL": doc_absolute_url,
            "wikiURL": absolutify(doc_absolute_url, for_wiki_site=True),
            "editURL": absolutify(
                reverse("wiki.edit", args=(doc.slug,), locale=doc.locale),
                for_wiki_site=True,
            ),
            "translateURL": (
                absolutify(
                    reverse("wiki.select_locale", args=(doc.slug,), locale=doc.locale),
                    for_wiki_site=True,
                )
                if doc.is_localizable
                else None
            ),
            "translationStatus": translation_status,
            "bodyHTML": doc.get_body_html(),
            "quickLinksHTML": doc.get_quick_links_html(),
            "tocHTML": doc.get_toc_html(),
            "raw": doc.html,
            "parents": [
                {"url": d.get_absolute_url(), "title": d.title} for d in doc.parents
            ],
            "translations": [
                {
                    "language": t.language,
                    "hrefLang": t.get_hreflang(available_locales),
                    "localizedLanguage": gettext(settings.LOCALES[t.locale].english),
                    "locale": t.locale,
                    "url": t.get_absolute_url(),
                    "title": t.title,
                }
                for t in other_translations
            ],
            "lastModified": (
                doc.current_revision and doc.current_revision.created.isoformat()
            ),
        },
        "redirectURL": None,
    }
