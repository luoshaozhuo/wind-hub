package cn.hedpy.scada.emslib;

import java.lang.reflect.InvocationTargetException;
import java.util.Set;
import java.lang.reflect.Method;

import cn.hedpy.scada.emslib.entity.FactoryPoints;
import cn.hedpy.scada.emslib.entity.WindTurbo;

/** Null-safety and regression harness for FactoryPoints checkIsLimit / calComOkValues. */
public class FactoryPointsNullTest {
    static int failures = 0;

    static void check(String name, boolean ok, Object detail) {
        if (ok) {
            System.out.println("PASS " + name);
        } else {
            failures++;
            System.out.println("FAIL " + name + " -> " + detail);
        }
    }

    static void setVal(Object holder, Object v) throws Exception {
        holder.getClass().getMethod("setVal", Object.class).invoke(holder, v);
    }

    static Object getVal(Object holder) throws Exception {
        return holder.getClass().getMethod("getVal").invoke(holder);
    }

    /** Invoke private checkIsLimit; returns Boolean or marks throwable. */
    static Object check(FactoryPoints fp, StringBuilder err) throws Exception {
        Method m = FactoryPoints.class.getDeclaredMethod("checkIsLimit");
        m.setAccessible(true);
        try {
            return m.invoke(fp);
        } catch (InvocationTargetException e) {
            err.append(e.getCause());
            return "THREW:" + e.getCause();
        }
    }

    static void callComOk(FactoryPoints fp, StringBuilder err) {
        try {
            Method m = FactoryPoints.class.getDeclaredMethod("calComOkValues");
            m.setAccessible(true);
            m.invoke(fp);
        } catch (InvocationTargetException e) {
            err.append(e.getCause());
            if (System.getProperty("fp.debug") != null) {
                e.getCause().printStackTrace(System.err);
            }
        } catch (Exception e) {
            err.append(e);
            if (System.getProperty("fp.debug") != null) {
                e.printStackTrace(System.err);
            }
        }
    }

    static WindTurbo wt(Double power, Boolean run, double cap) throws Exception {
        WindTurbo t = new WindTurbo();
        setVal(t.emsPowAct, power);
        setVal(t.emsRunState, run);
        setVal(t.capacity, cap);
        return t;
    }

    static void dispatch(FactoryPoints fp, Boolean a, Boolean b, Boolean c) throws Exception {
        setVal(fp.emsFacDispatchActEnabled, a);
        setVal(fp.emsGridDispatchActEnabled, b);
        setVal(fp.emsJkDispatchActEnabled, c);
    }

    static void doubles(FactoryPoints fp, Double send, Double loop) throws Exception {
        setVal(fp.emsActSend, send);
        setVal(fp.emsActLoopRef, loop);
        setVal(GlobalData.FactoryConfig.emsLimitCoe, 0.03d);
        setVal(GlobalData.FactoryConfig.fWFContractCapacity, 100d);
    }

    static double d(Object v) {
        return ((Number) v).doubleValue();
    }

    public static void main(String[] args) throws Exception {
        FactoryPoints fp = new FactoryPoints();
        StringBuilder err = new StringBuilder();

        // ---- checkIsLimit ----
        // 1 all booleans null, doubles valid
        dispatch(fp, null, null, null);
        doubles(fp, 0d, 0d);
        Object r = check(fp, err);
        check("cil_all_boolean_null_false", Boolean.FALSE.equals(r), String.valueOf(r) + " " + err);

        // 2 all false
        dispatch(fp, false, false, false);
        r = check(fp, err);
        check("cil_all_false", Boolean.FALSE.equals(r), r);

        // 3 one true (others null), within limit -> true
        dispatch(fp, null, true, null);
        doubles(fp, 0d, 0d);
        r = check(fp, err);
        check("cil_one_true_within_limit", Boolean.TRUE.equals(r), r);

        // 4 each Double null with dispatch enabled -> false, no throw
        String[] names = {"actSend", "actLoopRef", "limitCoe", "contractCapacity"};
        Double[] sends = {null, 0d, 0d, 0d};
        Double[] loops = {0d, null, 0d, 0d};
        Double[] coes = {0.03d, 0.03d, null, 0.03d};
        Double[] caps = {100d, 100d, 100d, null};
        for (int i = 0; i < 4; i++) {
            dispatch(fp, true, false, false);
            setVal(fp.emsActSend, sends[i]);
            setVal(fp.emsActLoopRef, loops[i]);
            setVal(GlobalData.FactoryConfig.emsLimitCoe, coes[i]);
            setVal(GlobalData.FactoryConfig.fWFContractCapacity, caps[i]);
            err.setLength(0);
            r = check(fp, err);
            check("cil_double_null_" + names[i], Boolean.FALSE.equals(r) && err.length() == 0,
                    r + " err=" + err);
        }

        // 5 all doubles valid, enabled, within -> true
        dispatch(fp, true, false, false);
        doubles(fp, 0d, 0d);
        r = check(fp, err);
        check("cil_enabled_within", Boolean.TRUE.equals(r), r);

        // 6 enabled but over limit -> false
        doubles(fp, 10d, 0d);
        r = check(fp, err);
        check("cil_enabled_over_limit", Boolean.FALSE.equals(r), r);

        // 7 dispatch disabled + doubles null -> false, no throw
        dispatch(fp, false, false, false);
        setVal(fp.emsActSend, null);
        setVal(fp.emsActLoopRef, null);
        setVal(GlobalData.FactoryConfig.emsLimitCoe, null);
        setVal(GlobalData.FactoryConfig.fWFContractCapacity, null);
        err.setLength(0);
        r = check(fp, err);
        check("cil_disabled_doubles_null", Boolean.FALSE.equals(r) && err.length() == 0,
                r + " err=" + err);

        // 8 boundary: actSend - loopRef == limitCoe*capacity -> false (strict <)
        dispatch(fp, true, false, false);
        doubles(fp, 3d, 0d); // 3 == 0.03*100
        r = check(fp, err);
        check("cil_boundary_strict_less", Boolean.FALSE.equals(r), r);
        doubles(fp, 2.999d, 0d);
        r = check(fp, err);
        check("cil_just_inside", Boolean.TRUE.equals(r), r);

        // ---- calComOkValues ----
        Set<WindTurbo> list = GlobalData.ComOkList;
        list.clear();

        // a empty list -> no NPE, zero stats
        err.setLength(0);
        callComOk(fp, err);
        check("cco_empty_list_no_npe", err.length() == 0, err);
        check("cco_empty_grid_count", d(getVal(fp.emsGridCount)) == 0d, getVal(fp.emsGridCount));
        check("cco_empty_act_sum", d(getVal(fp.emsFactoryActSum)) == 0d, getVal(fp.emsFactoryActSum));

        // b power null -> no NPE, contribution 0
        setVal(GlobalData.FactoryConfig.emsTurboOutPowerCalEnable, false);
        setVal(GlobalData.FactoryConfig.emsReactAvoidCurrentOverFlowEnable, false);
        list.add(wt(null, true, 50d));
        err.setLength(0);
        callComOk(fp, err);
        check("cco_power_null_no_npe", err.length() == 0, err);
        check("cco_power_null_sum_zero", d(getVal(fp.emsFactoryActSum)) == 0d,
                getVal(fp.emsFactoryActSum));

        // c power 0
        list.clear();
        list.add(wt(0d, true, 50d));
        err.setLength(0);
        callComOk(fp, err);
        check("cco_power_zero_sum_zero", err.length() == 0
                && d(getVal(fp.emsFactoryActSum)) == 0d, getVal(fp.emsFactoryActSum) + " " + err);

        // d negative power, config off -> raw included; config on -> truncated
        list.clear();
        list.add(wt(-500d, true, 50d));
        setVal(GlobalData.FactoryConfig.emsTurboOutPowerCalEnable, false);
        err.setLength(0);
        callComOk(fp, err);
        check("cco_negative_raw", err.length() == 0
                && d(getVal(fp.emsFactoryActSum)) == -500d, getVal(fp.emsFactoryActSum));
        setVal(GlobalData.FactoryConfig.emsTurboOutPowerCalEnable, true);
        err.setLength(0);
        callComOk(fp, err);
        check("cco_negative_truncated", err.length() == 0
                && d(getVal(fp.emsFactoryActSum)) == 0d, getVal(fp.emsFactoryActSum));
        check("cco_abs_sum_nonneg", d(getVal(fp.emsFactoryActSumAbs)) == 0d,
                getVal(fp.emsFactoryActSumAbs));
        setVal(GlobalData.FactoryConfig.emsTurboOutPowerCalEnable, false);

        // e positive power summed
        list.clear();
        list.add(wt(100d, true, 50d));
        err.setLength(0);
        callComOk(fp, err);
        check("cco_positive_sum", err.length() == 0
                && d(getVal(fp.emsFactoryActSum)) == 100d, getVal(fp.emsFactoryActSum));
        check("cco_positive_abs_sum", d(getVal(fp.emsFactoryActSumAbs)) == 100d,
                getVal(fp.emsFactoryActSumAbs));

        // f run state null -> not counted
        list.clear();
        list.add(wt(10d, null, 80d));
        err.setLength(0);
        callComOk(fp, err);
        check("cco_run_null_not_counted", err.length() == 0
                && d(getVal(fp.emsGridCount)) == 0d
                && d(getVal(fp.emsGridCapacity)) == 0d,
                "count=" + getVal(fp.emsGridCount) + " cap=" + getVal(fp.emsGridCapacity)
                        + " err=" + err);

        // g run false -> not counted
        list.clear();
        list.add(wt(10d, false, 80d));
        err.setLength(0);
        callComOk(fp, err);
        check("cco_run_false_not_counted", d(getVal(fp.emsGridCount)) == 0d,
                getVal(fp.emsGridCount));

        // h run true -> counted with capacity
        list.clear();
        list.add(wt(10d, true, 80d));
        err.setLength(0);
        callComOk(fp, err);
        check("cco_run_true_counted", err.length() == 0
                && d(getVal(fp.emsGridCount)) == 1d
                && d(getVal(fp.emsGridCapacity)) == 80d,
                "count=" + getVal(fp.emsGridCount) + " cap=" + getVal(fp.emsGridCapacity));

        // i mixed multi-turbine totals
        list.clear();
        list.add(wt(100d, true, 50d));   // counted, +100
        list.add(wt(null, false, 60d));  // not counted, +0
        list.add(wt(-30d, true, 70d));   // counted, -30 raw / 0 abs-truncated
        err.setLength(0);
        callComOk(fp, err);
        check("cco_mixed_no_npe", err.length() == 0, err);
        check("cco_mixed_act_sum", d(getVal(fp.emsFactoryActSum)) == 70d,
                getVal(fp.emsFactoryActSum));
        check("cco_mixed_abs_sum", d(getVal(fp.emsFactoryActSumAbs)) == 100d,
                getVal(fp.emsFactoryActSumAbs));
        check("cco_mixed_grid_count", d(getVal(fp.emsGridCount)) == 2d,
                getVal(fp.emsGridCount));
        check("cco_mixed_grid_capacity", d(getVal(fp.emsGridCapacity)) == 120d,
                getVal(fp.emsGridCapacity));
        check("cco_mixed_avg", Math.abs(d(getVal(fp.emsFactoryActAvg)) - 70d / 3d) < 1e-9,
                getVal(fp.emsFactoryActAvg));

        // j residual check: emsReactAvoidCurrentOverFlowEnable null
        setVal(GlobalData.FactoryConfig.emsReactAvoidCurrentOverFlowEnable, null);
        err.setLength(0);
        callComOk(fp, err);
        boolean residualNpe = err.length() > 0;
        System.out.println((residualNpe ? "RESIDUAL_NPE " : "RESIDUAL_OK ")
                + (err.length() == 0 ? "none" : err.toString().split("\n")[0]));
        setVal(GlobalData.FactoryConfig.emsReactAvoidCurrentOverFlowEnable, false);

        // restore sane state for repeat runs
        list.clear();

        if (failures > 0) {
            System.out.println("FAILURES=" + failures);
            System.exit(1);
        }
        System.out.println("ALL_CASES_PASS");
    }
}
