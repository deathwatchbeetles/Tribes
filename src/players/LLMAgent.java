package players;

import core.Types;
import core.actions.Action;
import core.actions.tribeactions.EndTurn;
import core.actors.Building;
import core.actors.City;
import core.actors.Tribe;
import core.actors.units.Unit;
import core.game.Board;
import core.game.GameState;
import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;
import utils.ElapsedCpuTimer;
import utils.Vector2d;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.util.ArrayList;
import java.util.List;

/**
 * Agent that delegates every decision to an LLM through the local HTTP bridge
 * (see llm_bridge/bridge.py).
 *
 * The engine calls {@link #act(GameState, ElapsedCpuTimer)} once per ACTION and keeps calling
 * until END_TURN is returned or no actions remain. Each call:
 *   1. collects the legal actions,
 *   2. serialises a compact, human-readable view of the state (turn, stars, score, tech,
 *      own cities/units, visible enemy cities/units, ASCII map of the visible area),
 *   3. POSTs it to http://127.0.0.1:8000/act with a 60 s timeout (-Dllm.bridge.timeout),
 *   4. returns the action at the index the bridge picked.
 *
 * Failure of any kind (bridge down, timeout, malformed reply, bad index) falls back to a safe
 * action so the game loop never crashes: END_TURN when it is legal, otherwise actions.get(0).
 */
public class LLMAgent extends Agent {

    public static final String BRIDGE_URL = System.getProperty("llm.bridge.url", "http://127.0.0.1:8000/act");
    public static final int TIMEOUT_SECONDS = Integer.getInteger("llm.bridge.timeout", 60);
    /** Print state summary + LLM reasoning to stdout every call. */
    public static final boolean LOG = !"false".equalsIgnoreCase(System.getProperty("llm.bridge.log", "true"));

    private final HttpClient http;
    private int actionCounterThisTurn = 0;
    private int lastTick = -1;
    private int calls = 0, fallbacks = 0;

    public LLMAgent(long seed) {
        super(seed);
        this.http = HttpClient.newBuilder()
                .connectTimeout(Duration.ofSeconds(3))
                .version(HttpClient.Version.HTTP_1_1)
                .build();
    }

    @Override
    public Agent copy() {
        return new LLMAgent(seed);
    }

    // ------------------------------------------------------------------------------------------
    // Main entry point
    // ------------------------------------------------------------------------------------------

    @Override
    public Action act(GameState gs, ElapsedCpuTimer ect) {
        ArrayList<Action> allActions = gs.getAllAvailableActions();
        int activeTribe = gs.getActiveTribeID();

        if (allActions == null || allActions.isEmpty()) {
            return new EndTurn(activeTribe);
        }

        // Filter self-harming actions (like the built-in SimpleAgent does) unless that empties the list.
        ArrayList<Action> actions = new ArrayList<>();
        for (Action a : allActions) {
            Types.ACTION t = a.getActionType();
            if (t != Types.ACTION.DESTROY && t != Types.ACTION.DISBAND) actions.add(a);
        }
        if (actions.isEmpty()) actions = allActions;

        // Per-turn bookkeeping for the prompt.
        if (gs.getTick() != lastTick) { lastTick = gs.getTick(); actionCounterThisTurn = 0; }
        actionCounterThisTurn++;
        calls++;

        Tribe me = gs.getTribe(playerID);
        JSONObject payload = new JSONObject();
        payload.put("turn", gs.getTick());
        payload.put("stars", me.getStars());
        payload.put("score", me.getScore());
        payload.put("player_id", playerID);
        payload.put("tribe", me.getType().toString());
        payload.put("action_number_this_turn", actionCounterThisTurn);
        payload.put("context", buildContext(gs, me));
        JSONArray arr = new JSONArray();
        for (Action a : actions) arr.put(describe(a, gs));
        payload.put("actions", arr);

        if (LOG) {
            System.out.println("\n[LLMAgent] ---- turn " + gs.getTick() + " | tribe " + me.getType()
                    + " | stars " + me.getStars() + " | score " + me.getScore()
                    + " | action #" + actionCounterThisTurn + " | " + actions.size() + " legal actions");
        }

        int idx = -1;
        String reasoning = "";
        try {
            HttpRequest request = HttpRequest.newBuilder()
                    .uri(URI.create(BRIDGE_URL))
                    .timeout(Duration.ofSeconds(TIMEOUT_SECONDS))
                    .header("Content-Type", "application/json")
                    .POST(HttpRequest.BodyPublishers.ofString(payload.toString()))
                    .build();
            HttpResponse<String> response = http.send(request, HttpResponse.BodyHandlers.ofString());
            if (response.statusCode() / 100 != 2) {
                throw new RuntimeException("bridge returned HTTP " + response.statusCode() + ": " + response.body());
            }
            JSONObject reply = new JSONObject(response.body());
            idx = reply.getInt("action_index");
            reasoning = reply.optString("reasoning", "");
        } catch (Exception e) {
            // Covers connect refused, timeouts (HttpTimeoutException), JSONException, InterruptedException...
            if (LOG) System.out.println("[LLMAgent] bridge call failed (" + e.getClass().getSimpleName()
                    + ": " + e.getMessage() + ") -> fallback");
            idx = -1;
        }

        Action chosen;
        if (idx >= 0 && idx < actions.size()) {
            chosen = actions.get(idx);
        } else {
            fallbacks++;
            if (LOG && idx != -1) System.out.println("[LLMAgent] action_index " + idx + " out of range -> fallback");
            chosen = safeFallback(actions);
            reasoning = "[fallback] " + reasoning;
        }

        if (LOG) {
            System.out.println("[LLMAgent] EXECUTE [" + actions.indexOf(chosen) + "] " + chosen
                    + (reasoning.isEmpty() ? "" : "\n[LLMAgent] reasoning: " + reasoning));
        }
        return chosen;
    }

    @Override
    public void result(GameState gs, double reward) {
        if (LOG) System.out.println("[LLMAgent] game over. reward=" + reward + " bridge calls=" + calls
                + " fallbacks=" + fallbacks + " final score=" + gs.getTribe(playerID).getScore());
    }

    /** END_TURN if legal (never harmful), else the first action, as the plan requires. */
    private Action safeFallback(ArrayList<Action> actions) {
        for (Action a : actions)
            if (a.getActionType() == Types.ACTION.END_TURN) return a;
        return actions.get(0);
    }

    // ------------------------------------------------------------------------------------------
    // Serialisation helpers
    // ------------------------------------------------------------------------------------------

    /** Action toString() plus a little extra so the LLM can reason about targets. */
    private String describe(Action a, GameState gs) {
        String s = a.toString();
        try {
            if (a instanceof core.actions.unitactions.Attack) {
                core.actions.unitactions.Attack at = (core.actions.unitactions.Attack) a;
                Unit target = (Unit) gs.getActor(at.getTargetId());
                if (target != null)
                    s += " (" + target.getType() + " of tribe " + target.getTribeId()
                            + " hp " + target.getCurrentHP() + "/" + target.getMaxHP() + " at " + target.getPosition() + ")";
            } else if (a instanceof core.actions.unitactions.Move) {
                Unit u = (Unit) gs.getActor(((core.actions.unitactions.Move) a).getUnitId());
                if (u != null) s += " (" + u.getType() + " from " + u.getPosition() + ")";
            }
        } catch (Exception ignored) { /* description is best-effort */ }
        return s;
    }

    private String buildContext(GameState gs, Tribe me) {
        StringBuilder sb = new StringBuilder(2048);
        Board board = gs.getBoard();
        int size = board.getSize();

        sb.append("Game mode: ").append(gs.getGameMode()).append(", map ").append(size).append("x").append(size)
          .append(". You are tribe ").append(playerID).append(" (").append(me.getType()).append(")")
          .append(", capital city id ").append(me.getCapitalID()).append(".\n");

        // Technologies
        sb.append("Researched tech: ");
        boolean any = false;
        for (Types.TECHNOLOGY t : Types.TECHNOLOGY.values()) {
            if (me.getTechTree().isResearched(t)) { sb.append(t).append(' '); any = true; }
        }
        if (!any) sb.append("(none)");
        sb.append("\n");

        // Own cities
        sb.append("\nYOUR CITIES:\n");
        for (City c : gs.getCities(playerID)) {
            if (c == null) continue;
            sb.append("  city ").append(c.getActorId()).append(c.isCapital() ? " (CAPITAL)" : "")
              .append(" at ").append(c.getPosition())
              .append(" level ").append(c.getLevel())
              .append(" pop ").append(c.getPopulation()).append('/').append(c.getPopulation_need())
              .append(" production ").append(c.getProduction())
              .append(" units ").append(c.getNumUnits()).append(c.hasWalls() ? " walls" : "");
            List<String> bl = new ArrayList<>();
            for (Building b : c.getBuildings()) bl.add(b.type + "@" + b.position);
            if (!bl.isEmpty()) sb.append(" buildings ").append(bl);
            sb.append("\n");
        }

        // Own units
        sb.append("\nYOUR UNITS:\n");
        ArrayList<Unit> myUnits = gs.getUnits(playerID);
        if (myUnits.isEmpty()) sb.append("  (none)\n");
        for (Unit u : myUnits) {
            if (u == null) continue;
            sb.append("  ").append(unitLine(u)).append(" status ").append(u.getStatus());
            Vector2d p = u.getPosition();
            if (board.getResourceAt(p.x, p.y) == Types.RESOURCE.RUINS)
                sb.append(u.isFresh() ? "  <-- ON RUINS: EXAMINE now, before moving it" : "  (on ruins; examine next turn while fresh)");
            else if (board.getTerrainAt(p.x, p.y) == Types.TERRAIN.VILLAGE)
                sb.append(u.isFresh() ? "  <-- ON VILLAGE: CAPTURE now, before moving it" : "  (on village; capture next turn while fresh)");
            sb.append("\n");
        }

        // Enemies (only what is visible to us)
        sb.append("\nVISIBLE ENEMY CITIES / UNITS:\n");
        boolean seen = false;
        for (Tribe other : gs.getTribes()) {
            if (other == null || other.getTribeId() == playerID) continue;
            sb.append("  tribe ").append(other.getTribeId()).append(" (").append(other.getType())
              .append(") score ").append(other.getScore()).append(":\n");
            for (Integer cid : other.getCitiesID()) {
                City c = (City) board.getActor(cid);
                if (c == null || !me.isVisible(c.getPosition().x, c.getPosition().y)) continue;
                seen = true;
                sb.append("    city ").append(c.getActorId()).append(c.isCapital() ? " (CAPITAL)" : "")
                  .append(" at ").append(c.getPosition()).append(" level ").append(c.getLevel())
                  .append(" units ").append(c.getNumUnits()).append(c.hasWalls() ? " walls" : "").append("\n");
            }
            for (Unit u : gs.getUnits(other.getTribeId())) {
                if (u == null || !me.isVisible(u.getPosition().x, u.getPosition().y)) continue;
                seen = true;
                sb.append("    ").append(unitLine(u)).append("\n");
            }
        }
        if (!seen) sb.append("  (nothing visible yet - explore)\n");

        // ASCII map of the visible area
        sb.append("\nMAP (row = y, col = x; coordinates are 'x : y'. Legend: . plain, f forest, m mountain, "
                + "s shallow water, d deep water, v village (capturable), c city tile, ? fog. "
                + "Resources: h fish, F fruit, a animal, w whales, o ore, C crops, r ruins. "
                + "Units: U yours, E enemy. Your city tiles: #, enemy city tiles: X)\n");
        sb.append("     ");
        for (int x = 0; x < size; x++) sb.append(String.format("%2d", x % 100)).append(' ');
        sb.append("\n");
        for (int y = 0; y < size; y++) {
            sb.append(String.format("y%2d  ", y));
            for (int x = 0; x < size; x++) {
                sb.append(' ').append(tileChar(gs, board, me, x, y)).append(' ');
            }
            sb.append("\n");
        }
        return sb.toString();
    }

    private String unitLine(Unit u) {
        return "unit " + u.getActorId() + " " + u.getType() + " at " + u.getPosition()
                + " hp " + u.getCurrentHP() + "/" + u.getMaxHP()
                + " atk " + u.ATK + " def " + u.DEF + " mov " + u.MOV + " range " + u.RANGE
                + (u.isVeteran() ? " veteran" : "");
    }

    private char tileChar(GameState gs, Board board, Tribe me, int x, int y) {
        if (!me.isVisible(x, y)) return '?';
        Unit u = board.getUnitAt(x, y);
        if (u != null) return u.getTribeId() == playerID ? 'U' : 'E';

        Types.TERRAIN terr = board.getTerrainAt(x, y);
        if (terr == Types.TERRAIN.CITY) {
            int cid = board.getCityIdAt(x, y);
            City c = cid >= 0 ? (City) board.getActor(cid) : null;
            return (c != null && c.getTribeId() == playerID) ? '#' : 'X';
        }
        if (terr == Types.TERRAIN.VILLAGE) return 'v';

        Types.RESOURCE res = board.getResourceAt(x, y);
        if (res != null) {
            switch (res) {
                case FISH: return 'h';
                case FRUIT: return 'F';
                case ANIMAL: return 'a';
                case WHALES: return 'w';
                case ORE: return 'o';
                case CROPS: return 'C';
                case RUINS: return 'r';
                default: break;
            }
        }
        if (terr == null) return '?';
        switch (terr) {
            case PLAIN: return '.';
            case FOREST: return 'f';
            case MOUNTAIN: return 'm';
            case SHALLOW_WATER: return 's';
            case DEEP_WATER: return 'd';
            case FOG: return '?';
            default: return terr.getMapChar();
        }
    }

    /** Unused but handy for debugging serialisation from a REPL. */
    public static String debugContext(GameState gs, int playerId) {
        LLMAgent a = new LLMAgent(0);
        a.setPlayerIDs(playerId, new ArrayList<>());
        return a.buildContext(gs, gs.getTribe(playerId));
    }
}
