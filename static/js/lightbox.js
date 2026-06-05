document.addEventListener("DOMContentLoaded", function () {
    var lightbox = document.getElementById("image-lightbox");
    var lightboxImage = document.getElementById("lightbox-image");
    var closeButton = document.getElementById("lightbox-close");

    if (!lightbox || !lightboxImage || !closeButton) {
        return;
    }

    var closeLightbox = function () {
        lightbox.classList.remove("open");
        lightbox.setAttribute("aria-hidden", "true");
        lightboxImage.removeAttribute("src");
        lightboxImage.removeAttribute("alt");
    };

    document.querySelectorAll("a.lightbox-trigger[href]").forEach(function (link) {
        link.addEventListener("click", function (event) {
            event.preventDefault();
            var imageElement = link.querySelector("img");
            lightboxImage.src = link.href;
            lightboxImage.alt = imageElement ? imageElement.alt : "Listing image";
            lightbox.classList.add("open");
            lightbox.setAttribute("aria-hidden", "false");
        });
    });

    closeButton.addEventListener("click", closeLightbox);
    lightbox.addEventListener("click", function (event) {
        if (event.target === lightbox) {
            closeLightbox();
        }
    });

    document.addEventListener("keydown", function (event) {
        if (event.key === "Escape" && lightbox.classList.contains("open")) {
            closeLightbox();
        }
    });
});
