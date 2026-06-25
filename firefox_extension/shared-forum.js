function isLikelyForumUrl(url) {
    try {
        const parsed = new URL(url);
        const host = parsed.hostname.toLowerCase();
        const path = parsed.pathname.toLowerCase();
        return (
            host.startsWith("forum.") ||
            host.startsWith("forums.") ||
            host.includes(".forum.") ||
            host.includes(".forums.") ||
            path.includes("/threads/") ||
            path.includes("/thread/") ||
            path.includes("/forums/") ||
            path.includes("/forum/")
        );
    } catch (_) {
        return false;
    }
}

function isLikelyForumHtml(html) {
    const sample = (html || "").slice(0, 300000).toLowerCase();
    return !!(
        sample.includes('data-template="thread_view"') ||
        sample.includes("xenforo") ||
        sample.includes("pagenav") ||
        sample.includes("bbwrapper") ||
        sample.includes("message--post") ||
        sample.includes("article class=\"message") ||
        (sample.includes("data-post-id") && sample.includes("cooked"))
    );
}
