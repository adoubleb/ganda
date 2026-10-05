// Line / branch / method coverage of the OrientDB jars from the fuzzer's JaCoCo
// execution data. Run through scripts/coverage_report.py, which builds the classpath
// (org.jacoco.core / org.jacoco.report / ASM from ~/.m2) and extracts the jars from the
// image; launched as a single-file source program, so there is nothing to build.
//
//   report --exec F... --lib DIR [--exclude C...] [--out DIR] [--sources DIR|JAR...] [--title T]
//   diff   --exec A... --base B... --lib DIR [--exclude C...] [--top N]
//
// Every com/orientechnologies class of every orientdb-*.jar in DIR is analyzed, loaded
// or not, one JaCoCo bundle per jar: the denominator is the whole engine, not the
// classes the campaign happened to load (which is what fuzzer/coverage.py can see).
// `diff` answers "what does A reach that B never does": the lines and methods A covers
// where B covers nothing, by package, the largest methods first.

import java.io.*;
import java.nio.file.*;
import java.util.*;
import java.util.zip.*;

import org.jacoco.core.analysis.*;
import org.jacoco.core.data.*;
import org.jacoco.core.tools.ExecFileLoader;
import org.jacoco.report.*;
import org.jacoco.report.csv.CSVFormatter;
import org.jacoco.report.html.HTMLFormatter;
import org.jacoco.report.xml.XMLFormatter;

public class CoverageReport {

    record ClassFile(String jar, String name, byte[] bytes) {}

    public static void main(String[] argv) throws Exception {
        if (argv.length == 0) usage();
        String cmd = argv[0];
        Map<String, List<String>> opt = parse(Arrays.copyOfRange(argv, 1, argv.length));
        List<ClassFile> classes = loadClasses(one(opt, "lib"), opt.getOrDefault("exclude", List.of()));
        switch (cmd) {
            case "report" -> report(opt, classes);
            case "diff" -> diff(opt, classes);
            default -> usage();
        }
    }

    static void usage() {
        System.err.println("usage: CoverageReport report --exec F... --lib DIR [--out DIR] [--sources P...] [--title T]\n"
                + "       CoverageReport diff --exec A... --base B... --lib DIR [--top N]");
        System.exit(2);
    }

    static Map<String, List<String>> parse(String[] args) {
        Map<String, List<String>> m = new HashMap<>();
        String key = null;
        for (String a : args) {
            if (a.startsWith("--")) {
                key = a.substring(2);
                m.computeIfAbsent(key, k -> new ArrayList<>());
            } else if (key != null) {
                m.get(key).add(a);
            }
        }
        return m;
    }

    static String one(Map<String, List<String>> opt, String key) {
        List<String> v = opt.get(key);
        if (v == null || v.isEmpty()) {
            System.err.println("missing --" + key);
            usage();
        }
        return v.get(0);
    }

    /** `exclude`: the agent's excludes (dotted class names), which it never probes. */
    static List<ClassFile> loadClasses(String lib, List<String> exclude) throws IOException {
        Set<String> skip = new HashSet<>();
        for (String x : exclude) skip.add(x.replace('.', '/'));
        List<Path> jars = new ArrayList<>();
        try (DirectoryStream<Path> ds = Files.newDirectoryStream(Paths.get(lib), "orientdb-*.jar")) {
            ds.forEach(jars::add);
        }
        Collections.sort(jars);
        List<ClassFile> out = new ArrayList<>();
        Set<String> seen = new HashSet<>();
        for (Path jar : jars) {
            String bundle = jar.getFileName().toString().replaceAll("-\\d[\\d.]*\\.jar$", "");
            try (ZipFile z = new ZipFile(jar.toFile())) {
                for (ZipEntry e : Collections.list(z.entries())) {
                    String n = e.getName();
                    if (!n.startsWith("com/orientechnologies/") || !n.endsWith(".class")) continue;
                    if (skip.contains(n.substring(0, n.length() - ".class".length()))) continue;
                    if (!seen.add(n)) {
                        System.err.println("skipping duplicate " + n + " in " + jar.getFileName());
                        continue;
                    }
                    try (InputStream in = z.getInputStream(e)) {
                        out.add(new ClassFile(bundle, n, in.readAllBytes()));
                    }
                }
            }
        }
        if (out.isEmpty()) throw new IOException("no com/orientechnologies classes in " + lib + "/orientdb-*.jar");
        return out;
    }

    static ExecFileLoader load(List<String> files) throws IOException {
        ExecFileLoader loader = new ExecFileLoader();
        for (String f : files) loader.load(new File(f));
        return loader;
    }

    /** One bundle per jar, analyzed against `store`. */
    static Map<String, IBundleCoverage> analyze(List<ClassFile> classes, ExecutionDataStore store) throws IOException {
        Map<String, CoverageBuilder> builders = new TreeMap<>();
        for (ClassFile c : classes) {
            CoverageBuilder b = builders.computeIfAbsent(c.jar(), k -> new CoverageBuilder());
            new Analyzer(store, b).analyzeClass(c.bytes(), c.jar() + "!" + c.name());
        }
        Map<String, IBundleCoverage> out = new LinkedHashMap<>();
        builders.forEach((jar, b) -> {
            out.put(jar, b.getBundle(jar));
            if (!b.getNoMatchClasses().isEmpty())
                System.err.println(jar + ": " + b.getNoMatchClasses().size() + " classes whose execution data does not match the jar (a different build?)");
        });
        return out;
    }

    // --- report -----------------------------------------------------------------

    static void report(Map<String, List<String>> opt, List<ClassFile> classes) throws IOException {
        ExecFileLoader loader = load(opt.getOrDefault("exec", List.of()));
        Map<String, IBundleCoverage> bundles = analyze(classes, loader.getExecutionDataStore());
        String title = opt.containsKey("title") ? String.join(" ", opt.get("title")) : "OrientDB";

        printTable("jar", bundles.values());
        List<IPackageCoverage> pkgs = new ArrayList<>();
        for (IBundleCoverage b : bundles.values()) pkgs.addAll(b.getPackages());
        pkgs.removeIf(p -> p.getLineCounter().getCoveredCount() == 0);
        pkgs.sort(Comparator.comparingInt((IPackageCoverage p) -> -p.getLineCounter().getCoveredCount()));
        printTable("package (lines covered, top 30)", pkgs.subList(0, Math.min(30, pkgs.size())));

        List<String> outDir = opt.get("out");
        if (outDir == null || outDir.isEmpty()) return;
        File dir = new File(outDir.get(0));
        dir.mkdirs();
        List<IReportVisitor> visitors = new ArrayList<>();
        visitors.add(new HTMLFormatter().createVisitor(new FileMultiReportOutput(new File(dir, "html"))));
        visitors.add(new CSVFormatter().createVisitor(new FileOutputStream(new File(dir, "coverage.csv"))));
        visitors.add(new XMLFormatter().createVisitor(new FileOutputStream(new File(dir, "coverage.xml"))));
        IReportVisitor v = new MultiReportVisitor(visitors);
        v.visitInfo(loader.getSessionInfoStore().getInfos(), loader.getExecutionDataStore().getContents());
        IReportGroupVisitor group = v.visitGroup(title);
        ISourceFileLocator sources = sources(opt.getOrDefault("sources", List.of()));
        for (IBundleCoverage b : bundles.values()) group.visitBundle(b, sources);
        v.visitEnd();
        System.out.println("report written to " + new File(dir, "html/index.html") + " (and coverage.csv, coverage.xml)");
    }

    static ISourceFileLocator sources(List<String> paths) throws IOException {
        MultiSourceFileLocator multi = new MultiSourceFileLocator(4);
        for (String p : paths) {
            File f = new File(p);
            if (f.isDirectory()) multi.add(new DirectorySourceFileLocator(f, "utf-8", 4));
            else multi.add(new ZipSourceFileLocator(new ZipFile(f)));
        }
        return multi;
    }

    /** A `-sources.jar` as a source locator (JaCoCo ships a directory one only). */
    static class ZipSourceFileLocator extends InputStreamSourceFileLocator {
        final ZipFile zip;

        ZipSourceFileLocator(ZipFile zip) {
            super("utf-8", 4);
            this.zip = zip;
        }

        @Override
        protected InputStream getSourceStream(String path) throws IOException {
            ZipEntry e = zip.getEntry(path);
            return e == null ? null : zip.getInputStream(e);
        }
    }

    static void printTable(String what, Collection<? extends ICoverageNode> nodes) {
        System.out.printf("%n%-58s %22s %22s %20s %16s%n", what, "lines", "branches", "methods", "classes");
        for (ICoverageNode n : nodes) System.out.println(row(n.getName().replace('/', '.'), n));
        if (nodes.size() > 1 && what.equals("jar")) {
            CoverageNodeImpl total = new CoverageNodeImpl(ICoverageNode.ElementType.GROUP, "total");
            nodes.forEach(total::increment);
            System.out.println(row("total", total));
        }
    }

    static String row(String name, ICoverageNode n) {
        return String.format("%-58s %22s %22s %20s %16s", name, cell(n.getLineCounter()), cell(n.getBranchCounter()), cell(n.getMethodCounter()), cell(n.getClassCounter()));
    }

    static String cell(ICounter c) {
        if (c.getTotalCount() == 0) return "-";
        return String.format("%d/%d %5.1f%%", c.getCoveredCount(), c.getTotalCount(), 100.0 * c.getCoveredRatio());
    }

    // --- diff -------------------------------------------------------------------

    record Only(String method, int lines, int instructions) {}

    static void diff(Map<String, List<String>> opt, List<ClassFile> classes) throws IOException {
        ExecutionDataStore a = load(opt.getOrDefault("exec", List.of())).getExecutionDataStore();
        ExecutionDataStore b = load(opt.getOrDefault("base", List.of())).getExecutionDataStore();
        int top = opt.containsKey("top") ? Integer.parseInt(opt.get("top").get(0)) : 40;
        Map<String, IClassCoverage> ca = byName(classes, a), cb = byName(classes, b);

        int onlyLines = 0, onlyBranches = 0, onlyMethods = 0;
        Map<String, int[]> perPackage = new TreeMap<>();
        List<Only> methods = new ArrayList<>();
        for (Map.Entry<String, IClassCoverage> e : ca.entrySet()) {
            IClassCoverage x = e.getValue(), y = cb.get(e.getKey());
            if (x.getInstructionCounter().getCoveredCount() == 0) continue;
            Map<String, IMethodCoverage> ym = new HashMap<>();
            for (IMethodCoverage m : y.getMethods()) ym.put(m.getName() + m.getDesc(), m);
            int[] pkg = perPackage.computeIfAbsent(x.getPackageName().replace('/', '.'), k -> new int[3]);
            for (IMethodCoverage m : x.getMethods()) {
                IMethodCoverage n = ym.get(m.getName() + m.getDesc());
                int lines = 0, branches = 0;
                for (int l = m.getFirstLine(); l >= 0 && l <= m.getLastLine(); l++) {
                    ILine la = m.getLine(l), lb = n.getLine(l);
                    if (covered(la.getStatus()) && !covered(lb.getStatus())) lines++;
                    branches += Math.max(0, la.getBranchCounter().getCoveredCount() - lb.getBranchCounter().getCoveredCount());
                }
                boolean methodOnly = m.getInstructionCounter().getCoveredCount() > 0 && n.getInstructionCounter().getCoveredCount() == 0;
                onlyLines += lines;
                onlyBranches += branches;
                pkg[0] += lines;
                pkg[1] += branches;
                if (methodOnly) {
                    onlyMethods++;
                    pkg[2]++;
                    methods.add(new Only(x.getName().replace('/', '.') + "." + m.getName(), lines, m.getInstructionCounter().getCoveredCount()));
                }
            }
        }
        System.out.printf("covered by --exec and never by --base: %d lines, %d branches, %d whole methods%n", onlyLines, onlyBranches, onlyMethods);
        System.out.printf("%n%-60s %8s %9s %8s%n", "package", "lines", "branches", "methods");
        perPackage.entrySet().stream()
                .filter(e -> e.getValue()[0] + e.getValue()[1] > 0)
                .sorted(Comparator.comparingInt((Map.Entry<String, int[]> e) -> -e.getValue()[0]))
                .forEach(e -> System.out.printf("%-60s %8d %9d %8d%n", e.getKey(), e.getValue()[0], e.getValue()[1], e.getValue()[2]));
        methods.sort(Comparator.comparingInt((Only o) -> -o.instructions()));
        System.out.printf("%nthe largest methods only --exec reaches (covered instructions, lines):%n");
        for (Only o : methods.subList(0, Math.min(top, methods.size())))
            System.out.printf("  %6d %5d  %s%n", o.instructions(), o.lines(), o.method());
    }

    static boolean covered(int status) {
        return status == ICounter.FULLY_COVERED || status == ICounter.PARTLY_COVERED;
    }

    static Map<String, IClassCoverage> byName(List<ClassFile> classes, ExecutionDataStore store) throws IOException {
        Map<String, IClassCoverage> out = new HashMap<>();
        for (IBundleCoverage bundle : analyze(classes, store).values())
            for (IPackageCoverage p : bundle.getPackages())
                for (IClassCoverage c : p.getClasses()) out.put(c.getName(), c);
        return out;
    }
}
