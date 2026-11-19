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
    const sample = (html || "").slice(0, 300000);
    return (
        /<[^>]+\bdata-template\s*=\s*["']thread_view["']/i.test(sample) ||
        /<article\b[^>]*\bclass\s*=\s*["'][^"']*\bmessage--post\b/i.test(sample) ||
        (/<li\b[^>]*\bclass\s*=\s*["'][^"']*\bmessage\b/i.test(sample) &&
            /<[^>]+\bclass\s*=\s*["'][^"']*\bmessageContent\b/i.test(sample)) ||
        (sample.includes("data-post-id") && sample.includes('class="cooked"'))
    );
}

function shouldCaptureForum(url, force, html) {
    try {
        const parsed = new URL(url);
        if (["youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com",
            "youtube-nocookie.com", "www.youtube-nocookie.com", "youtu.be"].includes(parsed.hostname) ||
            (parsed.hostname === "news.ycombinator.com" && /^\/item\/?$/.test(parsed.pathname))) {
            return false;
        }
    } catch (_) {
        return false;
    }
    return force || isLikelyForumUrl(url) || isLikelyForumHtml(html);
}

function forumPageNumberFromUrl(url) {
    if (!url) return null;
    const parsed = new URL(url);
    const route = parsed.search.startsWith("?threads/") ? parsed.search.split("&", 1)[0] : parsed.pathname;
    const match = parsed.search.match(/[?&]page=(\d+)(?:&|$)/i) || route.match(/\/page[-/](\d+)\/?$/i);
    return match ? Number(match[1]) : null;
}

function buildForumPageUrl(threadUrl, page) {
    const url = new URL(threadUrl);
    const suffix = /\/page[-/]\d+\/?$/i;
    const separator = /\/page\/\d+\/?$/i.test(url.pathname) ? "/page/" : "/page-";
    const [route, ...params] = url.search.slice(1).split("&");
    const threadRoute = route.startsWith("threads/");
    const usesPageParam = /[?&]page=\d+(?:&|$)/i.test(url.search) ||
        (url.search && !threadRoute && !suffix.test(url.pathname));
    const query = (threadRoute ? params : [route, ...params])
        .filter(value => value && !/^page=\d+$/i.test(value));
    url.hash = "";
    url.pathname = url.pathname.replace(suffix, "");
    if (threadRoute) {
        const baseRoute = route.replace(suffix, "").replace(/\/$/, "");
        query.unshift(page > 1 && !usesPageParam ? baseRoute + "/page-" + page : baseRoute);
    }
    if (page > 1 && usesPageParam) {
        query.push("page=" + page);
    } else if (page > 1 && !threadRoute) {
        url.pathname = url.pathname.replace(/\/$/, "") + separator + page;
    }
    url.search = query.join("&");
    return url.href;
}
