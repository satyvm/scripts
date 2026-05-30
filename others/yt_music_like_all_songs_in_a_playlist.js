async function likeAllSongs() {
  const scrollStep = 500;
  const delayBetweenActions = 700;

  let lastHeight = 0;
  let sameHeightCount = 0;
  const maxSameHeight = 3;

  let likedCount = 0;

  while (true) {
    window.scrollBy(0, scrollStep);
    await new Promise((r) => setTimeout(r, 100));

    const songs = document.querySelectorAll(
      "ytmusic-responsive-list-item-renderer",
    );

    for (let i = 0; i < songs.length; i++) {
      const song = songs[i];
      song.dispatchEvent(new MouseEvent("mouseover", { bubbles: true }));

      // Song-Infos
      const title =
        song.querySelector("yt-formatted-string.title")?.innerText ||
        "Unbekannt";
      const artist =
        song.querySelector("yt-formatted-string.subtitle")?.innerText ||
        "Unbekannt";

      // 🎯 Like-Button nur über aria-label "Mag ich" (Deutsch) oder "Like" (Englisch)
      const likeButton = song.querySelector(
        "button[aria-label='Mag ich'], button[aria-label='Like']",
      );
      if (!likeButton) {
        console.log(`⚠️ Kein Like-Button gefunden bei "${title}"`);
        continue;
      }

      // Prüfen, ob noch nicht geliked
      if (likeButton.getAttribute("aria-pressed") === "false") {
        likeButton.click();
        likedCount++;
        console.log(`✅ Liked: "${title}" von ${artist}`);
        await new Promise((r) => setTimeout(r, delayBetweenActions));
      } else {
        console.log(`⏩ Schon geliked: "${title}" von ${artist}`);
      }
    }

    // Ende prüfen
    const newHeight = document.body.scrollHeight;
    if (newHeight === lastHeight) {
      sameHeightCount++;
      if (sameHeightCount >= maxSameHeight) break;
    } else {
      sameHeightCount = 0;
    }
    lastHeight = newHeight;
  }

  console.log(
    `------------ COMPLETE | Insgesamt ${likedCount} Songs geliked ------------`,
  );
}

likeAllSongs();
