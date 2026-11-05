import { useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { CSSProperties, KeyboardEvent } from "react";
import { createPortal } from "react-dom";
import { Check, ChevronRight } from "lucide-react";
import type { DockPosition, DockTheme } from "../../stores/settingsStore";
import type { DockMenuRow } from "./dockModel";
import { placeMenu } from "./dockMotion";

// The dock's context menu: a glass menu portalled into document.body, generic over
// DockMenuRow. It is one list per open level: the menu itself, then the submenu open from one
// of its rows, and so on down. The active row holds DOM focus (roving), and the parent closes
// the menu by unmounting it when onClose fires. Events from inside bubble through the React
// tree to whoever rendered the menu, so keydown stops at the menu's root.

export type DockMenuCloseReason = "select" | "escape" | "tab" | "outside" | "dismiss";

interface Rect {
  left: number;
  top: number;
  right: number;
  bottom: number;
}

interface DockMenuProps {
  rows: DockMenuRow[];
  anchor: Rect; // the item the menu belongs to, in viewport px
  side: DockPosition; // the screen edge the dock is on
  theme: DockTheme;
  accentColor: string;
  onClose: (reason: DockMenuCloseReason) => void;
  initialFocus?: "first" | "none"; // "none" leaves the highlight off and focuses the list itself
}

type ItemRow = Extract<DockMenuRow, { kind: "item" }>;

// Where keyboard focus sits: a row of a level, or the level's list when index is null.
// `pointer` marks a move that came from the mouse, which must not scroll the list.
interface Cursor {
  level: number;
  index: number | null;
  pointer?: boolean;
}

const VIEWPORT_MARGIN = 8; // closest a submenu gets to the viewport edge, as for the menu itself
const SUBMENU_LIP = 5; // a submenu starts this far above its row: the list padding
const SUBMENU_OVERLAP = 4; // a submenu tucks this far under its parent's edge
const GLASS = ".dock-menu-list, .dock-menu-tail"; // the layers that carry the backdrop blur

function isEnabled(row: DockMenuRow | undefined): row is ItemRow {
  return row?.kind === "item" && !row.disabled;
}

// The next enabled row after `from` in direction `dir`, wrapping round. A null `from` starts
// at the matching end, so it also finds the first or last enabled row. Null when none is.
function stepIndex(rows: DockMenuRow[], from: number | null, dir: 1 | -1): number | null {
  const count = rows.length;
  let index = from ?? (dir === 1 ? -1 : count);
  for (let tries = 0; tries < count; tries += 1) {
    index = (index + dir + count) % count;
    if (isEnabled(rows[index])) return index;
  }
  return null;
}

// The rows of every open level: the menu's own, then each submenu along `path` (the index of
// the row that is open on each level). A path that no longer leads to a submenu stops early.
function resolveLevels(rows: DockMenuRow[], path: number[]): DockMenuRow[][] {
  const levels = [rows];
  for (const index of path) {
    const row = levels[levels.length - 1][index];
    if (!isEnabled(row) || !row.items) break;
    levels.push(row.items);
  }
  return levels;
}

function samePath(a: number[], b: number[]): boolean {
  return a.length === b.length && a.every((value, i) => value === b[i]);
}

// Where the cursor goes when the rows change under it. It stays while its row is still there
// and enabled, or while it is on a list. Otherwise it goes to the nearest enabled row above
// that place, wrapping round, or to the list when no row is enabled. If its level is gone, it
// goes up to the deepest level left, starting from the row that opened the next one.
function settleCursor(cursor: Cursor, levels: DockMenuRow[][], path: number[]): Cursor {
  const level = Math.min(cursor.level, levels.length - 1);
  const index = level === cursor.level ? cursor.index : (path[level] ?? null);
  const rows = levels[level];
  if (index === null || isEnabled(rows[index])) {
    return level === cursor.level && index === cursor.index ? cursor : { level, index };
  }
  return { level, index: stepIndex(rows, Math.min(index, rows.length), -1) };
}

// Levels, lists and rows are found in the DOM instead of through refs: the menu owns this
// markup, and focus and placement are imperative anyway.
function levelEl(root: HTMLElement, level: number): HTMLElement | null {
  return root.querySelectorAll<HTMLElement>(".dock-menu")[level] ?? null;
}

function listEl(wrap: HTMLElement): HTMLElement | null {
  return wrap.querySelector<HTMLElement>(".dock-menu-list");
}

function rowEl(wrap: HTMLElement, index: number): HTMLElement | null {
  return wrap.querySelector<HTMLElement>(`[data-row="${index}"]`);
}

// A submenu opens beside its parent menu, to the right or, with no room there, to the left,
// with its first row level with the row that opened it (`rowTop`). No tail.
function placeSubmenu(
  parent: { left: number; right: number },
  rowTop: number,
  size: { width: number; height: number },
  viewport: { width: number; height: number },
): { x: number; y: number; maxHeight: number; flipped: boolean } {
  const maxHeight = viewport.height - 2 * VIEWPORT_MARGIN;
  const rightX = parent.right - SUBMENU_OVERLAP;
  const flipped = rightX + size.width > viewport.width - VIEWPORT_MARGIN;
  const leftX = Math.max(VIEWPORT_MARGIN, parent.left + SUBMENU_OVERLAP - size.width);
  // A menu taller than the viewport is capped by maxHeight and then sits at the top margin.
  const lowest = viewport.height - VIEWPORT_MARGIN - size.height;
  const y = Math.max(VIEWPORT_MARGIN, Math.min(lowest, rowTop - SUBMENU_LIP));
  return { x: flipped ? leftX : rightX, y, maxHeight, flipped };
}

// ===== Motion =====

function reducedMotion(): boolean {
  return globalThis.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;
}

// A level opens by growing in from the tail (or from its parent row) while its glass layers
// fade in. The layers fade one by one because a faded ancestor would stop their backdrop blur
// from seeing the page behind. Returns a cleanup; does nothing where animate() is missing.
function playEnter(wrap: HTMLElement, submenu: boolean): (() => void) | undefined {
  if (typeof wrap.animate !== "function") return undefined;
  const reduced = reducedMotion();
  const runs: Animation[] = [];
  if (!reduced) {
    runs.push(
      wrap.animate(
        [{ transform: `scale(${submenu ? 0.94 : 0.82})` }, { transform: "scale(1)" }],
        { duration: submenu ? 220 : 340, easing: "cubic-bezier(0.2, 1.25, 0.4, 1)" },
      ),
    );
  }
  for (const layer of wrap.querySelectorAll<HTMLElement>(GLASS)) {
    runs.push(
      layer.animate([{ opacity: 0 }, { opacity: 1 }], {
        duration: reduced ? 100 : submenu ? 120 : 160,
        easing: "ease-out",
      }),
    );
  }
  return () => runs.forEach((run) => run.cancel());
}

// The menu closes at once: onClose runs and the parent unmounts it. The fade-out plays on a
// copy of the menu that is inert and goes away when it ends, so it never delays the close.
function playExit(root: HTMLElement): void {
  if (typeof root.animate !== "function") return;
  const ghost = root.cloneNode(true) as HTMLElement;
  ghost.removeAttribute("data-dock-menu");
  ghost.setAttribute("aria-hidden", "true");
  ghost.inert = true;
  document.body.append(ghost);
  // A copy starts unscrolled, so a scrolled list would flash its top rows while it fades. Its
  // lists take their scroll positions now that they are in the page.
  const lists = root.querySelectorAll<HTMLElement>(".dock-menu-list");
  ghost.querySelectorAll<HTMLElement>(".dock-menu-list").forEach((list, index) => {
    list.scrollTop = lists[index].scrollTop;
  });

  const reduced = reducedMotion();
  const runs: Animation[] = [];
  if (!reduced) {
    for (const wrap of ghost.querySelectorAll<HTMLElement>(".dock-menu")) {
      runs.push(
        wrap.animate([{ transform: "scale(1)" }, { transform: "scale(0.96)" }], {
          duration: 150,
          easing: "ease-in",
          fill: "forwards",
        }),
      );
    }
  }
  for (const layer of ghost.querySelectorAll<HTMLElement>(GLASS)) {
    runs.push(
      layer.animate([{ opacity: 1 }, { opacity: 0 }], {
        duration: reduced ? 100 : 150,
        easing: "ease-in",
        fill: "forwards",
      }),
    );
  }
  void Promise.allSettled(runs.map((run) => run.finished)).then(() => ghost.remove());
}

// ===== Panel =====

interface PanelProps {
  rows: DockMenuRow[];
  level: number;
  side: DockPosition;
  idPrefix: string; // row ids are `${idPrefix}-${level}-${index}`
  labelledBy?: string; // a submenu is named by the row that opened it
  highlighted: number | null;
  expanded: number | null; // the row whose submenu is open
  onKeyDown: (e: KeyboardEvent<HTMLElement>) => void;
  onHover: (index: number) => void;
  onPick: (index: number) => void;
}

// One open level: a glass list of rows and, on the first level, the tail that points at the
// item. Positioning is DockMenu's job; this plays the opening animation.
function MenuPanel({
  rows,
  level,
  side,
  idPrefix,
  labelledBy,
  highlighted,
  expanded,
  onKeyDown,
  onHover,
  onPick,
}: PanelProps) {
  const wrapRef = useRef<HTMLDivElement>(null);

  useLayoutEffect(
    () => (wrapRef.current ? playEnter(wrapRef.current, level > 0) : undefined),
    [level],
  );

  return (
    <div ref={wrapRef} className="dock-menu" data-side={level === 0 ? side : undefined}>
      <div
        className="dock-menu-list"
        role="menu"
        tabIndex={-1}
        aria-labelledby={labelledBy}
        onKeyDown={onKeyDown}
      >
        <span className="dock-menu-highlight" aria-hidden="true" />
        {rows.map((row, index) => {
          if (row.kind === "separator") {
            return <div key={index} className="dock-menu-separator" role="separator" />;
          }
          const Icon = row.icon;
          return (
            <div
              key={index}
              id={`${idPrefix}-${level}-${index}`}
              className="dock-menu-row"
              role={row.checked === undefined ? "menuitem" : "menuitemcheckbox"}
              tabIndex={-1}
              data-row={index}
              data-active={highlighted === index ? "" : undefined}
              aria-checked={row.checked}
              aria-disabled={row.disabled ? true : undefined}
              aria-haspopup={row.items ? "menu" : undefined}
              aria-expanded={row.items ? expanded === index : undefined}
              onMouseMove={() => onHover(index)}
              onClick={() => onPick(index)}
            >
              <span className="dock-menu-check" aria-hidden="true">
                {row.checked && <Check size={13} aria-hidden="true" />}
              </span>
              {Icon && (
                <span className="dock-menu-icon" aria-hidden="true">
                  <Icon size={15} />
                </span>
              )}
              <span className="dock-menu-label">{row.label}</span>
              {row.shortcut && <span className="dock-menu-shortcut">{row.shortcut}</span>}
              {row.items && (
                <ChevronRight className="dock-menu-chevron" size={12} aria-hidden="true" />
              )}
            </div>
          );
        })}
      </div>
      {level === 0 && <span className="dock-menu-tail" aria-hidden="true" />}
    </div>
  );
}

// ===== Menu =====

export default function DockMenu({
  rows,
  anchor,
  side,
  theme,
  accentColor,
  onClose,
  initialFocus = "first",
}: DockMenuProps) {
  const idPrefix = useId();
  const rootRef = useRef<HTMLDivElement>(null);
  const leaving = useRef(false); // set once the exit animation has started, so it plays once
  // openPath[level] is the row of that level whose submenu is open.
  const [openPath, setOpenPath] = useState<number[]>([]);
  const [cursor, setCursor] = useState<Cursor>(() => ({
    level: 0,
    index: initialFocus === "first" ? stepIndex(rows, null, 1) : null,
  }));

  const levels = useMemo(() => resolveLevels(rows, openPath), [rows, openPath]);
  // The rows change while the menu is open when the windows do (Cmd+W closes one, say). The
  // cursor then settles on a row that is left, and keyboard focus follows it, instead of
  // staying with a row that is gone and falling out of the menu.
  const [seenRows, setSeenRows] = useState(rows);
  if (rows !== seenRows) {
    setSeenRows(rows);
    const settled = settleCursor(cursor, levels, openPath);
    if (settled !== cursor) setCursor(settled);
  }
  // The row lit on each level: the one under the cursor, or on the way to it the row whose
  // submenu is open.
  const highlighted = useMemo(
    () =>
      levels.map((_, level) =>
        cursor.level === level ? cursor.index : levels.length > level + 1 ? openPath[level] : null,
      ),
    [levels, cursor, openPath],
  );

  const close = (reason: DockMenuCloseReason) => {
    if (rootRef.current && !leaving.current) {
      leaving.current = true;
      playExit(rootRef.current);
    }
    onClose(reason);
  };
  // The window listeners below outlive a render, so they call the latest close.
  const closeRef = useRef(close);
  useEffect(() => {
    closeRef.current = close;
  });

  useEffect(() => {
    const dismiss = () => closeRef.current("dismiss");
    const onPointerDown = (e: PointerEvent) => {
      if (e.target instanceof Node && rootRef.current?.contains(e.target)) return;
      closeRef.current("outside");
    };
    // Capture, so a click that something else swallows still closes the menu.
    document.addEventListener("pointerdown", onPointerDown, true);
    window.addEventListener("blur", dismiss);
    window.addEventListener("resize", dismiss);
    window.addEventListener("scroll", dismiss);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown, true);
      window.removeEventListener("blur", dismiss);
      window.removeEventListener("resize", dismiss);
      window.removeEventListener("scroll", dismiss);
    };
  }, []);

  // Place every open level before the browser paints. A level is measured at its natural
  // height first; placeMenu (or placeSubmenu) then says where it goes and how tall it may be.
  // A submenu is placed from where its parent level was just placed and from layout offsets,
  // not from getBoundingClientRect, which would include the opening animation's scale.
  const { left, top, right, bottom } = anchor;
  useLayoutEffect(() => {
    const root = rootRef.current;
    if (!root) return;
    const viewport = { width: window.innerWidth, height: window.innerHeight };
    // The level before this one, as just placed.
    let parentBox: { left: number; right: number; top: number } | null = null;
    for (let level = 0; level < levels.length; level += 1) {
      const wrap = levelEl(root, level);
      const list = wrap && listEl(wrap);
      if (!wrap || !list) break;
      const scrolled = list.scrollTop;
      list.style.maxHeight = "";
      const size = { width: wrap.offsetWidth, height: wrap.offsetHeight };
      // With no room beyond the anchor (a bottom dock's item near the top of the viewport, say)
      // the cap comes out at 0 or below; only a positive one applies.
      const cap = (maxHeight: number) => {
        if (maxHeight > 0) list.style.maxHeight = `${maxHeight}px`;
      };
      if (level === 0) {
        const placed = placeMenu({ left, top, right, bottom }, size, side, viewport);
        wrap.style.left = `${placed.x}px`;
        wrap.style.top = `${placed.y}px`;
        wrap.style.setProperty("--dock-menu-tail", `${placed.tail}px`);
        cap(placed.maxHeight);
        parentBox = { left: placed.x, right: placed.x + size.width, top: placed.y };
      } else {
        const parentWrap = levelEl(root, level - 1);
        const parentList = parentWrap && listEl(parentWrap);
        const opener = parentWrap && rowEl(parentWrap, openPath[level - 1]);
        if (!parentBox || !parentList || !opener) break;
        const rowTop = parentBox.top + opener.offsetTop - parentList.scrollTop;
        const placed = placeSubmenu(parentBox, rowTop, size, viewport);
        wrap.style.left = `${placed.x}px`;
        wrap.style.top = `${placed.y}px`;
        wrap.style.transformOrigin = `${placed.flipped ? "100%" : "0"} ${SUBMENU_LIP}px`;
        cap(placed.maxHeight);
        parentBox = { left: placed.x, right: placed.x + size.width, top: placed.y };
      }
      list.scrollTop = scrolled;
    }
  }, [levels, openPath, left, top, right, bottom, side]);

  // The highlight is one bar per level that slides to the lit row. It is set without the
  // transition the first time, so it appears on its row instead of sliding in from the top.
  useLayoutEffect(() => {
    const root = rootRef.current;
    if (!root) return;
    highlighted.forEach((index, level) => {
      const wrap = levelEl(root, level);
      const bar = wrap && listEl(wrap)?.querySelector<HTMLElement>(".dock-menu-highlight");
      const row = wrap && index !== null ? rowEl(wrap, index) : null;
      if (!bar) return;
      if (!row) {
        bar.style.opacity = "0";
        return;
      }
      const first = bar.dataset.placed !== "true";
      if (first) bar.style.transition = "none";
      bar.style.transform = `translateY(${row.offsetTop}px)`;
      bar.style.height = `${row.offsetHeight}px`;
      bar.style.opacity = "1";
      if (first) {
        void bar.offsetHeight; // commit the jump before the transition comes back
        bar.style.transition = "";
        bar.dataset.placed = "true";
      }
    });
  }, [highlighted]);

  // Keyboard focus follows the cursor.
  useEffect(() => {
    const root = rootRef.current;
    const wrap = root && levelEl(root, cursor.level);
    const list = wrap && listEl(wrap);
    const target = (wrap && cursor.index !== null ? rowEl(wrap, cursor.index) : null) ?? list;
    target?.focus({ preventScroll: cursor.pointer });
  }, [cursor]);

  const setPath = (next: number[]) => setOpenPath((prev) => (samePath(prev, next) ? prev : next));

  // Moves the cursor within a level; submenus opened from rows beside it close.
  const move = (level: number, index: number | null) => {
    setPath(openPath.slice(0, level));
    setCursor({ level, index });
  };

  // `focus` is for the keyboard, which goes into the submenu; a click or hover leaves the
  // cursor on the row that opened it.
  const openSubmenu = (level: number, index: number, focus: boolean) => {
    const row = levels[level][index];
    setPath([...openPath.slice(0, level), index]);
    if (focus && isEnabled(row) && row.items) {
      setCursor({ level: level + 1, index: stepIndex(row.items, null, 1) });
    } else {
      setCursor({ level, index });
    }
  };

  // Closes the submenu at `level` and puts focus back on the row that opened it.
  const closeSubmenu = (level: number) => {
    setPath(openPath.slice(0, level - 1));
    setCursor({ level: level - 1, index: openPath[level - 1] ?? null });
  };

  // The pointer lights the row under it, opens its submenu if it has one and closes the
  // rest. A disabled row lights nothing.
  const hover = (level: number, index: number) => {
    const row = levels[level][index];
    const enabled = isEnabled(row);
    const lit = enabled ? index : null;
    const kept = openPath.slice(0, level);
    const nextPath = enabled && row.items ? [...kept, index] : kept;
    if (cursor.level === level && cursor.index === lit && samePath(openPath, nextPath)) return;
    setPath(nextPath);
    setCursor({ level, index: lit, pointer: true });
  };

  const pick = (level: number, index: number, byKey: boolean) => {
    const row = levels[level][index];
    if (!isEnabled(row)) return;
    if (row.items) {
      openSubmenu(level, index, byKey);
      return;
    }
    try {
      row.onSelect?.();
    } finally {
      close("select"); // the menu closes even if the action throws
    }
  };

  const handleKey = (e: KeyboardEvent<HTMLElement>, level: number) => {
    const here = levels[level];
    const target = e.target instanceof Element ? e.target.closest<HTMLElement>("[data-row]") : null;
    const index = target ? Number(target.dataset.row) : null;
    const row = index === null ? undefined : here[index];
    switch (e.key) {
      case "ArrowDown":
        move(level, stepIndex(here, index, 1));
        break;
      case "ArrowUp":
        move(level, stepIndex(here, index, -1));
        break;
      case "Home":
        move(level, stepIndex(here, null, 1));
        break;
      case "End":
        move(level, stepIndex(here, null, -1));
        break;
      case "ArrowRight":
        if (index !== null && isEnabled(row) && row.items) openSubmenu(level, index, true);
        break;
      case "ArrowLeft":
        if (level > 0) closeSubmenu(level);
        break;
      case "Escape":
        if (level > 0) closeSubmenu(level);
        else close("escape");
        break;
      case "Tab":
        // Tab only closes; the parent puts focus back, and the browser must not move it on.
        close("tab");
        break;
      case "Enter":
      case " ":
        if (index !== null) pick(level, index, true);
        break;
      default:
        return;
    }
    e.preventDefault();
  };

  return createPortal(
    <div
      ref={rootRef}
      className="dock-menu-root"
      data-dock-menu=""
      data-theme={theme}
      style={{ "--dock-menu-accent": accentColor } as CSSProperties}
      onKeyDown={(e) => e.stopPropagation()}
      // Keep focus on the active row when the mouse goes down on the menu, and no native menu.
      onMouseDown={(e) => e.preventDefault()}
      onContextMenu={(e) => e.preventDefault()}
    >
      {levels.map((levelRows, level) => (
        <MenuPanel
          key={`${level}-${openPath[level - 1] ?? ""}`}
          rows={levelRows}
          level={level}
          side={side}
          idPrefix={idPrefix}
          labelledBy={level > 0 ? `${idPrefix}-${level - 1}-${openPath[level - 1]}` : undefined}
          highlighted={highlighted[level]}
          expanded={levels.length > level + 1 ? openPath[level] : null}
          onKeyDown={(e) => handleKey(e, level)}
          onHover={(index) => hover(level, index)}
          onPick={(index) => pick(level, index, false)}
        />
      ))}
    </div>,
    document.body,
  );
}
