/**
 * Thumbnail Preview Cycling Engine
 *
 * Cycles through YouTube auto-generated numbered thumbnails on hover (desktop)
 * or scroll-into-view (tablet). Supports dynamic cards via MutationObserver.
 *
 * Usage:
 *   initThumbPreview({ containerId, cardSelector, thumbClass })
 */
(function() {
    'use strict';

    // YouTube HQ numbered thumbnails (auto-generated at ~25%, 50%, 75%)
    var THUMB_NAMES = ['hq1', 'hq2', 'hq3'];
    var CYCLE_INTERVAL = 1200; // ms between frames
    var MIN_VALID_WIDTH = 200; // real HQ thumbs are 480x360; placeholders are 120x90

    // Shared state: only one card previews at a time
    var activeCard = null;
    var cycleTimer = null;
    var previewCache = {}; // videoId -> [valid thumb URLs]

    function getVideoId(card) {
        return card.dataset.videoId || '';
    }

    function getThumbWrap(card) {
        return card.querySelector('.thumbnail-wrap');
    }

    function preloadThumbs(videoId, callback) {
        if (previewCache[videoId]) {
            callback(previewCache[videoId]);
            return;
        }
        var urls = THUMB_NAMES.map(function(name) {
            return 'https://i.ytimg.com/vi/' + videoId + '/' + name + '.jpg';
        });
        var valid = [];
        var pending = urls.length;

        urls.forEach(function(url) {
            var img = new Image();
            img.onload = function() {
                if (img.naturalWidth > MIN_VALID_WIDTH) {
                    valid.push(url);
                }
                if (--pending === 0) {
                    previewCache[videoId] = valid;
                    callback(valid);
                }
            };
            img.onerror = function() {
                if (--pending === 0) {
                    previewCache[videoId] = valid;
                    callback(valid);
                }
            };
            img.src = url;
        });
    }

    function startPreview(card, thumbClass) {
        if (activeCard === card) return;
        stopPreview();
        activeCard = card;

        var videoId = getVideoId(card);
        if (!videoId) return;

        var wrap = getThumbWrap(card);
        if (!wrap) return;

        var origThumb = card.querySelector('.' + thumbClass);
        if (!origThumb) return;
        var origSrc = origThumb.src;

        preloadThumbs(videoId, function(validUrls) {
            if (activeCard !== card || validUrls.length === 0) return;

            // Build frame list: original + numbered thumbs
            var frames = [origSrc].concat(validUrls);
            var frameIndex = 0;

            // Create overlay image
            var overlay = document.createElement('img');
            overlay.className = 'thumb-preview';
            overlay.alt = '';
            wrap.appendChild(overlay);

            // Create dots
            var dotsWrap = document.createElement('div');
            dotsWrap.className = 'thumb-dots';
            frames.forEach(function(_, i) {
                var dot = document.createElement('span');
                dot.className = 'thumb-dot' + (i === 0 ? ' active' : '');
                dotsWrap.appendChild(dot);
            });
            wrap.appendChild(dotsWrap);
            wrap.classList.add('previewing');

            function showFrame(idx) {
                frameIndex = idx;
                if (idx === 0) {
                    // Show original — hide overlay
                    overlay.classList.remove('visible');
                } else {
                    overlay.src = frames[idx];
                    overlay.classList.add('visible');
                }
                var dots = dotsWrap.children;
                for (var i = 0; i < dots.length; i++) {
                    dots[i].classList.toggle('active', i === idx);
                }
            }

            // Start cycling from frame 1 (first numbered thumb)
            showFrame(1);

            cycleTimer = setInterval(function() {
                if (activeCard !== card) { stopPreview(); return; }
                var next = (frameIndex + 1) % frames.length;
                showFrame(next);
            }, CYCLE_INTERVAL);

            // Store cleanup refs on wrap
            wrap._previewCleanup = function() {
                clearInterval(cycleTimer);
                cycleTimer = null;
                overlay.remove();
                dotsWrap.remove();
                wrap.classList.remove('previewing');
                delete wrap._previewCleanup;
            };
        });
    }

    function stopPreview() {
        if (!activeCard) return;
        var wrap = getThumbWrap(activeCard);
        if (wrap && wrap._previewCleanup) {
            wrap._previewCleanup();
        }
        if (cycleTimer) {
            clearInterval(cycleTimer);
            cycleTimer = null;
        }
        activeCard = null;
    }

    window.initThumbPreview = function(config) {
        var container = document.getElementById(config.containerId);
        if (!container) return;

        var isTouch = window.matchMedia('(pointer: coarse)').matches;

        if (!isTouch) {
            // Desktop: event delegation via mouseover/mouseout (these bubble, unlike mouseenter/mouseleave)
            container.addEventListener('mouseover', function(e) {
                var wrap = e.target.closest('.thumbnail-wrap');
                if (!wrap) return;
                var card = wrap.closest(config.cardSelector);
                if (card) startPreview(card, config.thumbClass);
            });

            container.addEventListener('mouseout', function(e) {
                var wrap = e.target.closest('.thumbnail-wrap');
                if (!wrap) return;
                // Only stop if mouse actually left the thumbnail-wrap (not just moved between children)
                var related = e.relatedTarget;
                if (related && wrap.contains(related)) return;
                var card = wrap.closest(config.cardSelector);
                if (card && card === activeCard) stopPreview();
            });
        } else {
            // Tablet: IntersectionObserver at 70% threshold
            var observer = new IntersectionObserver(function(entries) {
                entries.forEach(function(entry) {
                    var card = entry.target;
                    if (entry.isIntersecting && entry.intersectionRatio >= 0.7) {
                        startPreview(card, config.thumbClass);
                    } else if (card === activeCard) {
                        stopPreview();
                    }
                });
            }, { threshold: [0, 0.7] });

            // Observe existing cards
            container.querySelectorAll(config.cardSelector).forEach(function(card) {
                observer.observe(card);
            });

            // Watch for dynamically added cards
            var mutObs = new MutationObserver(function(mutations) {
                mutations.forEach(function(m) {
                    m.addedNodes.forEach(function(node) {
                        if (node.nodeType === 1 && node.matches(config.cardSelector)) {
                            observer.observe(node);
                        }
                    });
                });
            });
            mutObs.observe(container, { childList: true });
        }
    };
})();
