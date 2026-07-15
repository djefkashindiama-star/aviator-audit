(() => {
  "use strict";

  if (!location.hostname.endsWith(".aviator.studio")) return;

  let lastSignature = "";

  function findCrashHistory(value, seen, depth = 0) {
    if (!value || typeof value !== "object" || depth > 6 || seen.has(value)) return null;
    seen.add(value);
    if (Array.isArray(value)) {
      if (
        value.length >= 2 &&
        value.every(
          (item) =>
            item &&
            typeof item === "object" &&
            typeof item._id === "string" &&
            Number.isFinite(item.multiplierCrash)
        )
      ) {
        return value;
      }
      for (const item of value.slice(0, 40)) {
        const found = findCrashHistory(item, seen, depth + 1);
        if (found) return found;
      }
      return null;
    }
    for (const item of Object.values(value)) {
      const found = findCrashHistory(item, seen, depth + 1);
      if (found) return found;
    }
    return null;
  }

  function reactRootFiber() {
    const root = document.querySelector("#root");
    if (!root) return null;
    const key = Object.keys(root).find((name) => name.startsWith("__reactContainer$"));
    const container = key ? root[key] : null;
    return container?.stateNode?.current || container || null;
  }

  function currentHistory() {
    const start = reactRootFiber();
    if (!start) return [];
    const fibers = [start];
    let visited = 0;
    while (fibers.length && visited++ < 5000) {
      const fiber = fibers.pop();
      let hook = fiber.memoizedState;
      let hookCount = 0;
      while (hook && hookCount++ < 40) {
        const found = findCrashHistory(hook.memoizedState, new WeakSet());
        if (found) {
          return found.slice(0, 100).map((item) => ({
            id: item._id,
            multiplier: item.multiplierCrash
          }));
        }
        hook = hook.next;
      }
      if (fiber.child) fibers.push(fiber.child);
      if (fiber.sibling) fibers.push(fiber.sibling);
    }
    return [];
  }

  function publish() {
    const history = currentHistory();
    if (history.length < 2) return;
    const signature = history.slice(0, 12).map((item) => item.id).join("|");
    if (!signature || signature === lastSignature) return;
    lastSignature = signature;
    window.postMessage({ source: "aviator-audit-probe", history }, "*");
  }

  publish();
  setInterval(publish, 750);
})();
