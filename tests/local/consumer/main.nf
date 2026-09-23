/*
 * The local FUSE tier's consumer pipeline: tasks that read a mounted trio the way a pipeline's tasks do,
 * from a manifest the plugin wrote, staged into the task and read inside Apptainer.
 *
 *   --mode parity  copies the three members out and runs plink2 --missing --freq on them
 *   --mode ladder  runs --consumers concurrent plink2 --freq tasks on the same trio
 *   --mode scan    reads the whole .bed once, sequentially
 *
 * The PLINK 2 processes run in the image given by --plink2_image; COPY_MEMBERS runs on the host.
 */

params.manifest = null
params.outdir = null
params.mode = 'parity'
params.consumers = 1
params.plink2_image = null

process COPY_MEMBERS {
    publishDir params.outdir, mode: 'copy'

    input:
    tuple path(bed), path(bim), path(fam)

    output:
    path 'mounted.*'

    script:
    """
    cat ${bed} > mounted.bed
    cat ${bim} > mounted.bim
    cat ${fam} > mounted.fam
    """
}

process SCAN_BED {
    input:
    tuple path(bed), path(bim), path(fam)

    output:
    stdout

    script:
    """
    cat ${bed} | wc -c
    """
}

process PLINK2_STATS {
    container params.plink2_image
    publishDir params.outdir, mode: 'copy'

    input:
    tuple path(bed), path(bim), path(fam)

    output:
    path 'plink2_mounted.*'

    script:
    """
    plink2 --bfile ${bed.baseName} --missing --freq --out plink2_mounted
    """
}

process PLINK2_FREQ {
    container params.plink2_image
    publishDir params.outdir, mode: 'copy'

    input:
    tuple val(consumer), path(bed), path(bim), path(fam)

    output:
    path "c_${consumer}.afreq"

    script:
    """
    plink2 --bfile ${bed.baseName} --freq --out c_${consumer}
    """
}

workflow {
    trio = channel.fromPath(params.manifest)
        .splitCsv(header: true)
        .map { row -> tuple(file(row.bed), file(row.bim), file(row.fam)) }
    if( params.mode == 'parity' ) {
        COPY_MEMBERS(trio)
        PLINK2_STATS(trio)
    }
    else if( params.mode == 'scan' ) {
        SCAN_BED(trio)
    }
    else {
        PLINK2_FREQ(channel.of(1..(params.consumers as int)).combine(trio))
    }
}
