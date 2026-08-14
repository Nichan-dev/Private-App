// Service Worker สำหรับ Web Push เท่านั้น (ไม่ได้ทำ offline caching)
// รับ push event -> เปิดหา ชื่อเล่นของคนที่ทักมาจาก IndexedDB (ถ้าตั้งไว้) -> เด้งแจ้งเตือน

function openFriendsDB() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open("chatAppDB", 1);
    req.onupgradeneeded = () => {
      if (!req.result.objectStoreNames.contains("friends")) {
        req.result.createObjectStore("friends", { keyPath: "code" });
      }
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

function getFriendNickname(code) {
  return openFriendsDB()
    .then(
      (db) =>
        new Promise((resolve) => {
          const tx = db.transaction("friends", "readonly");
          const req = tx.objectStore("friends").get(code);
          req.onsuccess = () => resolve(req.result ? req.result.nickname : null);
          req.onerror = () => resolve(null);
        })
    )
    .catch(() => null);
}

self.addEventListener("push", (event) => {
  let payload = {};
  try {
    payload = event.data ? event.data.json() : {};
  } catch (e) {
    payload = {};
  }

  if (payload.type !== "friend_message" || !payload.code) return;
  const code = payload.code;

  event.waitUntil(
    getFriendNickname(code).then((nickname) => {
      const title = nickname && nickname.trim() ? nickname : code;
      return self.registration.showNotification(title, {
        body: "ทักคุณมา",
        icon: "/static/icons/icon-192.png",
        badge: "/static/icons/badge-96.png",
        tag: "friend-message-" + code,
        renotify: true,
        data: { code },
      });
    })
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  event.waitUntil(
    clients.matchAll({ type: "window", includeUncontrolled: true }).then((windowClients) => {
      for (const client of windowClients) {
        if ("focus" in client) return client.focus();
      }
      if (clients.openWindow) return clients.openWindow("/");
    })
  );
});
