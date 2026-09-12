// Click-to-play YouTube facade: no YouTube iframe (and no YouTube cookies/JS)
// loads until the viewer explicitly clicks. Works with mkdocs-material's
// instant-navigation, which replaces the DOM without a full page load.
document.addEventListener("click", function (event) {
  const facade = event.target.closest(".ki-video[data-video-id]");
  if (!facade || facade.querySelector("iframe")) return;

  const id = facade.getAttribute("data-video-id");
  const iframe = document.createElement("iframe");
  iframe.src = `https://www.youtube-nocookie.com/embed/${id}?autoplay=1&rel=0`;
  iframe.title = facade.getAttribute("data-video-title") || "YouTube video player";
  iframe.allow =
    "accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture";
  iframe.allowFullscreen = true;
  facade.replaceChildren(iframe);
});
