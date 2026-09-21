// The 80-number Keno pick board -- same "build once, mutate only the
// classList of the cell that changed" discipline as render/board.js
// (spec: "no heavy animation libraries... 60fps on a low-end Android").
// Unlike board.js (a passive call-history display), this board is also
// the picker itself: tapping a cell toggles it in/out of the current
// ticket's selection, so this module owns click/keyboard wiring too,
// not just rendering.
//
// Four states, distinguishable by shape as well as colour (spec 13:
// "colour-blind safe" -- see css/keno.css for the actual shape/icon
// rules this classList selection drives):
//   plain      -- not picked, not drawn
//   selected   -- this ticket's own pick, not drawn yet
//   drawn      -- came up in the draw, not one of this ticket's picks
//   matched    -- drawn AND selected (a hit)

const POOL_SIZE = 80;
const COLUMNS = 8;

let cellsByNumber = new Map();

export function buildBoard(container, onToggle) {
  container.innerHTML = "";
  cellsByNumber = new Map();
  const grid = document.createElement("div");
  grid.className = "keno-board";
  grid.style.setProperty("--keno-columns", String(COLUMNS));

  for (let number = 1; number <= POOL_SIZE; number++) {
    const cell = document.createElement("div");
    cell.className = "keno-cell";
    cell.textContent = String(number);
    cell.tabIndex = 0;
    cell.setAttribute("role", "button");
    cell.setAttribute("aria-label", String(number));
    const activate = () => onToggle(number);
    cell.addEventListener("click", activate);
    cell.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        activate();
      }
    });
    grid.appendChild(cell);
    cellsByNumber.set(number, cell);
  }
  container.appendChild(grid);
}

export function setSelected(numbers) {
  for (const [number, cell] of cellsByNumber) {
    cell.classList.toggle("selected", numbers.has(number));
  }
}

export function markDrawn(number) {
  const cell = cellsByNumber.get(number);
  if (!cell) return;
  cell.classList.add("drawn");
  if (cell.classList.contains("selected")) cell.classList.add("matched");
}

export function markAllDrawn(numbers) {
  for (const number of numbers) markDrawn(number);
}

// Called once a ticket is placed (picks are locked in for the round) --
// selection taps must stop landing on a board that's already committed.
export function setLocked(locked) {
  for (const cell of cellsByNumber.values()) {
    cell.classList.toggle("locked", locked);
  }
}

// New round: clear the drawn/matched trail but not the player's
// selection (Repeat and back-to-back rounds both rely on the picks
// surviving a resetDraw()).
export function resetDraw() {
  for (const cell of cellsByNumber.values()) {
    cell.classList.remove("drawn", "matched");
  }
}

export function resetSelection() {
  for (const cell of cellsByNumber.values()) {
    cell.classList.remove("selected");
  }
}
