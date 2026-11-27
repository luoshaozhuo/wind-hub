package cn.hedpy.scada.emslib;

import java.lang.reflect.Field;
import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Method;

import cn.hedpy.scada.emslib.entity.WindTurbo;

/** Null-safety + semantics harness for WindTurbo reactStateCheck / workmode statistics. */
public class WindTurboNullTest {
    static int failures = 0;

    static void setVal(Object holder, Object v) throws Exception {
        holder.getClass().getMethod("setVal", Object.class).invoke(holder, v);
    }

    static Object getVal(Object holder) throws Exception {
        return holder.getClass().getMethod("getVal").invoke(holder);
    }

    static Object val(Object holder) {
        try {
            return getVal(holder);
        } catch (Exception e) {
            return "GETVAL_ERR:" + e;
        }
    }

    static Object call(WindTurbo t, String method, StringBuilder err) {
        try {
            Method m = WindTurbo.class.getDeclaredMethod(method);
            m.setAccessible(true);
            return m.invoke(t);
        } catch (InvocationTargetException e) {
            err.append(e.getCause());
            if (System.getProperty("fp.debug") != null) {
                e.getCause().printStackTrace(System.err);
            }
            return "THREW:" + e.getCause();
        } catch (Exception e) {
            err.append(e);
            return "ERR:" + e;
        }
    }

    static void fireWorkMode(WindTurbo t, Object mode, StringBuilder err) {
        try {
            Method m = WindTurbo.class.getDeclaredMethod("emsWorkModeUpdate");
            m.setAccessible(true);
            m.invoke(t); // registers listener (also done by ctor; re-register is additive-safe for this test)
            Method upd = t.emsWorkMode.getClass().getMethod("update", Object.class);
            upd.invoke(t.emsWorkMode, mode);
        } catch (InvocationTargetException e) {
            err.append(e.getCause());
            if (System.getProperty("fp.debug") != null) {
                e.getCause().printStackTrace(System.err);
            }
        } catch (Exception e) {
            err.append(e);
        }
    }

    static void setPrivateField(Object t, String name, Object value) throws Exception {
        Field f = WindTurbo.class.getDeclaredField(name);
        f.setAccessible(true);
        Object holder = f.get(t);
        if (holder != null && holder.getClass().getName().contains(".Value")) {
            holder.getClass().getMethod("setVal", Object.class).invoke(holder, value);
        } else {
            f.set(t, value);
        }
    }

    static WindTurbo turbine() throws Exception {
        WindTurbo t = new WindTurbo();
        setVal(t.capacity, 50000d);
        setVal(t.emsPowAct, 1000d);
        setVal(t.emsPowReact, 100d);
        return t;
    }

    static String obs(WindTurbo t, String... fields) throws Exception {
        StringBuilder sb = new StringBuilder();
        for (String name : fields) {
            Field f = WindTurbo.class.getDeclaredField(name);
            f.setAccessible(true);
            sb.append(name).append("=").append(val(f.get(t))).append(";");
        }
        return sb.toString();
    }

    static void check(String name, boolean ok, Object detail) {
        if (ok) {
            System.out.println("PASS " + name);
        } else {
            failures++;
            System.out.println("FAIL " + name + " -> " + detail);
        }
    }

    public static void main(String[] args) throws Exception {
        // ---------- reactStateCheck ----------
        StringBuilder err = new StringBuilder();
        WindTurbo t = turbine();
        setVal(t.emsWorkMode, null);
        call(t, "reactStateCheck", err);
        check("rsc_workmode_null_no_npe", err.length() == 0, err);

        err.setLength(0);
        t = turbine();
        setVal(t.emsWorkMode, 4);
        call(t, "reactStateCheck", err);
        check("rsc_workmode_4_no_npe", err.length() == 0, err);
        System.out.println("OBS rsc_mode4 " + obs(t, "emsReaRun", "emsFaultStateYunNan", "emsConverterRatio"));

        err.setLength(0);
        t = turbine();
        setVal(t.emsWorkMode, 8);
        call(t, "reactStateCheck", err);
        check("rsc_workmode_8_no_npe", err.length() == 0, err);
        System.out.println("OBS rsc_mode8 " + obs(t, "emsReaRun", "emsFaultStateYunNan"));

        err.setLength(0);
        t = turbine();
        setVal(t.emsWorkMode, 16);
        call(t, "reactStateCheck", err);
        check("rsc_workmode_16_no_npe", err.length() == 0, err);

        err.setLength(0);
        t = turbine();
        setVal(t.emsWorkMode, 32);
        setVal(t.isSample, true);
        call(t, "reactStateCheck", err);
        check("rsc_workmode_32_no_npe", err.length() == 0, err);
        System.out.println("OBS rsc_mode32 " + obs(t, "emsReaRun"));

        err.setLength(0);
        t = turbine();
        setVal(t.emsWorkMode, 64);
        call(t, "reactStateCheck", err);
        check("rsc_workmode_64_no_npe", err.length() == 0, err);

        err.setLength(0);
        t = turbine();
        setVal(t.emsWorkMode, 32);
        setVal(t.isSample, null);
        call(t, "reactStateCheck", err);
        check("rsc_isSample_null_no_npe", err.length() == 0, err);
        System.out.println("OBS rsc_issample_null " + obs(t, "emsReaRun"));

        // ---------- emsWorkModeUpdate listener ----------
        err.setLength(0);
        t = turbine();
        setVal(t.emsComState, false);
        fireWorkMode(t, 32, err);
        check("wmu_comstate_false_no_npe", err.length() == 0, err);

        err.setLength(0);
        t = turbine();
        setVal(t.emsComState, null);
        fireWorkMode(t, 32, err);
        check("wmu_comstate_null_no_npe", err.length() == 0, err);
        System.out.println("OBS wmu_comstate_null " + obs(t, "emsRunState", "emsTurboStatus"));

        err.setLength(0);
        t = turbine();
        setVal(t.emsComState, true);
        setVal(t.emsFaultState, null);
        fireWorkMode(t, 4, err);
        check("wmu_faultstate_null_no_npe", err.length() == 0, err);
        System.out.println("OBS wmu_faultstate_null " + obs(t, "emsFaultOrMainState", "emsTurboStatus", "emsRunStateForShanDong"));

        err.setLength(0);
        t = turbine();
        setVal(t.emsComState, true);
        setVal(t.emsFaultState, false);
        setVal(t.emsWaitStateShanDong, false);
        setVal(t.emsWorkMode, 32);
        fireWorkMode(t, 32, err);
        check("wmu_mode32_grid", err.length() == 0
                && Boolean.TRUE.equals(val(t.emsRunState)), err + " run=" + val(t.emsRunState));
        System.out.println("OBS wmu_mode32 " + obs(t, "emsRunState", "emsTurboStatus", "emsRunStateForShanDong", "inertiaLimitEnable"));

        err.setLength(0);
        t = turbine();
        setVal(t.emsComState, true);
        setVal(t.emsFaultState, true);
        setVal(t.emsWorkMode, 64);
        fireWorkMode(t, 64, err);
        check("wmu_mode64_fault", err.length() == 0
                && Boolean.TRUE.equals(val(t.emsFaultOrMainState)), err + " fault=" + val(t.emsFaultOrMainState));
        System.out.println("OBS wmu_mode64 " + obs(t, "emsFaultOrMainState", "emsTurboStatus", "emsFaultStateYunNan"));

        err.setLength(0);
        t = turbine();
        setVal(t.emsComState, true);
        setVal(t.emsFaultState, false);
        setVal(t.emsWorkMode, 8);
        fireWorkMode(t, 8, err);
        check("wmu_mode8_no_npe", err.length() == 0, err);
        System.out.println("OBS wmu_mode8 " + obs(t, "emsRunState", "emsTurboStatus"));

        // ---------- calRunStateShanDong ----------
        err.setLength(0);
        t = turbine();
        setVal(t.emsComState, null);
        call(t, "calRunStateShanDong", err);
        check("rsd_comstate_null_no_npe", err.length() == 0, err);
        System.out.println("OBS rsd_comstate_null " + obs(t, "emsRunStateForShanDong", "emsCapacityShanDong"));

        err.setLength(0);
        t = turbine();
        setVal(t.emsComState, true);
        setVal(t.emsWaitStateShanDong, false);
        setVal(t.emsWorkMode, 32);
        call(t, "calRunStateShanDong", err);
        check("rsd_mode32_grid_state", err.length() == 0
                && Integer.valueOf(1).equals(val(t.emsRunStateForShanDong)),
                err + " state=" + val(t.emsRunStateForShanDong));
        System.out.println("OBS rsd_mode32 " + obs(t, "emsRunStateForShanDong", "emsCapacityShanDong", "emsPowerActShanDong"));

        err.setLength(0);
        t = turbine();
        setVal(t.emsComState, true);
        setVal(t.emsWorkMode, 4);
        call(t, "calRunStateShanDong", err);
        check("rsd_mode4_no_npe", err.length() == 0, err);

        err.setLength(0);
        t = turbine();
        setVal(t.emsComState, true);
        setVal(t.emsWorkMode, null);
        call(t, "calRunStateShanDong", err);
        check("rsd_mode_null_no_npe", err.length() == 0, err);

        err.setLength(0);
        t = turbine();
        setVal(t.emsComState, true);
        setVal(t.emsWorkMode, 32);
        setVal(t.emsPowAct, null);
        setVal(t.emsPowReact, null);
        call(t, "calRunStateShanDong", err);
        check("rsd_power_null_no_npe", err.length() == 0, err);
        System.out.println("OBS rsd_power_null " + obs(t, "emsRunStateForShanDong", "emsPowerActShanDong"));

        // ---------- calInertiaAlterState ----------
        err.setLength(0);
        t = turbine();
        setVal(t.emsWorkMode, null);
        call(t, "calInertiaAlterState", err);
        check("cia_mode_null_no_npe", err.length() == 0, err);
        System.out.println("OBS cia_mode_null " + obs(t, "inertiaLimitEnable"));

        err.setLength(0);
        t = turbine();
        setVal(t.emsWorkMode, 32);
        setVal(t.emsPowAct, null);
        call(t, "calInertiaAlterState", err);
        check("cia_power_null_no_npe", err.length() == 0, err);
        System.out.println("OBS cia_power_null " + obs(t, "inertiaLimitEnable"));

        err.setLength(0);
        t = turbine();
        setVal(t.emsWorkMode, 32);
        setVal(t.emsPowAct, 40000d);
        setVal(t.emsAutoLimitState, false);
        call(t, "calInertiaAlterState", err);
        check("cia_power_high_can_alter", err.length() == 0
                && Boolean.TRUE.equals(val(t.inertiaLimitEnable)),
                err + " enable=" + val(t.inertiaLimitEnable));
        System.out.println("OBS cia_power_high " + obs(t, "inertiaLimitEnable"));

        // ---------- checkWinIsOk ----------
        err.setLength(0);
        t = turbine();
        setVal(t.emsComState, null);
        setVal(t.emsWorkMode, 32);
        Object r = call(t, "checkWinIsOk", err);
        check("cwi_comstate_null_false", err.length() == 0 && Boolean.FALSE.equals(r), r + " " + err);
        System.out.println("OBS cwi_comstate_null " + r);

        err.setLength(0);
        t = turbine();
        setVal(t.emsComState, false);
        setVal(t.emsWorkMode, 32);
        r = call(t, "checkWinIsOk", err);
        check("cwi_comstate_false_false", err.length() == 0 && Boolean.FALSE.equals(r), r);

        err.setLength(0);
        t = turbine();
        setVal(t.emsComState, true);
        setVal(t.emsWorkMode, 32);
        call(t, "checkWinIsOk", err); // first call arms `started` and stamps timers
        long past = System.currentTimeMillis() - 60_000;
        setPrivateField(t, "recentNoGridTm", Long.valueOf(past));
        setPrivateField(t, "recentNoLimitTm", Long.valueOf(past));
        r = call(t, "checkWinIsOk", err);
        check("cwi_grid_ready_true", err.length() == 0 && Boolean.TRUE.equals(r), r + " " + err);
        System.out.println("OBS cwi_ready " + r);

        err.setLength(0);
        t = turbine();
        setVal(t.emsComState, true);
        setVal(t.emsWorkMode, 4);
        setPrivateField(t, "recentNoGridTm", Long.valueOf(past));
        setPrivateField(t, "recentNoLimitTm", Long.valueOf(past));
        r = call(t, "checkWinIsOk", err);
        check("cwi_mode4_false", err.length() == 0 && Boolean.FALSE.equals(r), r);

        // ---------- reactStateCheck branches reached at runtime ----------
        err.setLength(0);
        t = turbine();
        setVal(t.emsLocalOnOff, true);
        setVal(t.emsWorkMode, 32);
        setVal(t.emsPowReact, null);
        setVal(t.emsReactPowerMaxSetFeedback, null);
        call(t, "reactStateCheck", err);
        check("rsc_localonoff_null_power_no_npe", err.length() == 0, err);
        System.out.println("OBS rsc_localonoff " + obs(t, "emsReaRun", "emsReactSetLimitTurbo"));

        err.setLength(0);
        t = turbine();
        setVal(t.forbidReActPower, true);
        setVal(t.emsPowReact, null);
        call(t, "reactStateCheck", err);
        check("rsc_forbid_null_power_no_npe", err.length() == 0, err);

        err.setLength(0);
        t = turbine();
        setVal(GlobalData.FactoryConfig.emsReactCanCtrlEnable, true);
        setVal(t.emsReactCanCtrl, false);
        setVal(t.emsPowReact, null);
        setVal(t.emsReactPowerMaxSetFeedback, null);
        call(t, "reactStateCheck", err);
        check("rsc_cancrl_null_power_no_npe", err.length() == 0, err);

        err.setLength(0);
        t = turbine();
        setVal(t.emsWorkMode, 32);
        setVal(GlobalData.FactoryConfig.iCtrlReaLogicPoint, 1);
        setVal(t.emsPowAct, null);
        setVal(t.cos, null);
        call(t, "reactStateCheck", err);
        check("rsc_realogo1_null_power_no_npe", err.length() == 0, err);

        err.setLength(0);
        t = turbine();
        setVal(t.emsWorkMode, 32);
        setVal(GlobalData.FactoryConfig.iCtrlReaLogicPoint, 0);
        setVal(t.emsPowAct, null);
        call(t, "reactStateCheck", err);
        check("rsc_realogo0_null_power_no_npe", err.length() == 0, err);

        // ---------- multi-turbine mix ----------
        WindTurbo a = turbine(); setVal(a.emsComState, true); setVal(a.emsWaitStateShanDong, false); setVal(a.emsWorkMode, 32);
        WindTurbo b = turbine(); setVal(b.emsComState, true); setVal(b.emsWorkMode, 4);
        WindTurbo c = turbine(); setVal(c.emsComState, null); setVal(c.emsWorkMode, null);
        StringBuilder mixErr = new StringBuilder();
        call(a, "calRunStateShanDong", mixErr);
        call(b, "calRunStateShanDong", mixErr);
        call(c, "calRunStateShanDong", mixErr);
        check("mix_three_turbines_no_npe", mixErr.length() == 0, mixErr);
        check("mix_grid_turbine_state_1", Integer.valueOf(1).equals(val(a.emsRunStateForShanDong)),
                val(a.emsRunStateForShanDong));
        check("mix_offgrid_turbine_not_1", !Integer.valueOf(1).equals(val(b.emsRunStateForShanDong)),
                val(b.emsRunStateForShanDong));
        check("mix_null_com_turbine_state_4", Integer.valueOf(4).equals(val(c.emsRunStateForShanDong)),
                val(c.emsRunStateForShanDong));
        System.out.println("OBS mix a=" + val(a.emsRunStateForShanDong)
                + " b=" + val(b.emsRunStateForShanDong) + " c=" + val(c.emsRunStateForShanDong));

        if (failures > 0) {
            System.out.println("FAILURES=" + failures);
            System.exit(1);
        }
        System.out.println("ALL_CASES_PASS");
    }
}
