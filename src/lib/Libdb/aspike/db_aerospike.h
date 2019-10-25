/*
 * Copyright (C) 1994-2018 Altair Engineering, Inc.
 * For more information, contact Altair at www.altair.com.
 *
 * This file is part of the PBS Professional ("PBS Pro") software.
 *
 * Open Source License Information:
 *
 * PBS Pro is free software. You can redistribute it and/or modify it under the
 * terms of the GNU Affero General Public License as published by the Free
 * Software Foundation, either version 3 of the License, or (at your option) any
 * later version.
 *
 * PBS Pro is distributed in the hope that it will be useful, but WITHOUT ANY
 * WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
 * FOR A PARTICULAR PURPOSE.
 * See the GNU Affero General Public License for more details.
 *
 * You should have received a copy of the GNU Affero General Public License
 * along with this program.  If not, see <http://www.gnu.org/licenses/>.
 *
 * Commercial License Information:
 *
 * For a copy of the commercial license terms and conditions,
 * go to: (http://www.pbspro.com/UserArea/agreement.html)
 * or contact the Altair Legal Department.
 *
 * Altair’s dual-license business model allows companies, individuals, and
 * organizations to create proprietary derivative works of PBS Pro and
 * distribute them - whether embedded or bundled with other software -
 * under a commercial license agreement.
 *
 * Use of Altair’s trademarks, including but not limited to "PBS™",
 * "PBS Professional®", and "PBS Pro™" and Altair’s logos is subject to Altair's
 * trademark licensing policies.
 *
 */

/**
 * @file    db_aerospike.h
 *
 * @brief
 *  aerospike specific implementation
 *
 * This header file contains aerospike specific data structures and functions
 * to access the PBS aerospike database. These structures are used only by the
 * aerospike specific data store implementation, and should not be used directly
 * by the rest of the PBS code.
 *
 * The functions/interfaces in this header are PBS Private.
 */

#ifndef _DB_AEROSPIKE_H
#define	_DB_AEROSPIKE_H

#ifdef	__cplusplus
extern "C" {
#endif


#include <aerospike/aerospike.h>
#include <aerospike/aerospike_key.h>
#include <aerospike/as_arraylist.h>
#include <aerospike/as_error.h>
#include <aerospike/as_hashmap.h>
#include <aerospike/as_hashmap_iterator.h>
#include <aerospike/as_nil.h>
#include <aerospike/as_map_operations.h>
#include <aerospike/as_record.h>
#include <aerospike/aerospike_index.h>
#include <aerospike/as_record_iterator.h>
#include <aerospike/as_status.h>
#include <aerospike/as_val.h>
#include <aerospike/as_query.h>
#include <aerospike/aerospike_query.h>
#include <netinet/in.h>

#ifndef WIN32
#include <sys/types.h>
#include <inttypes.h>
#endif
#include "net_connect.h"
#include "list_link.h"
#include "portability.h"
#include "attribute.h"
#include <sys/time.h>

/*
 * Conversion macros for long long type
 */
#if !defined(ntohll)
#define ntohll(x) pbs_ntohll(x)
#endif
#if !defined(htonll)
#define htonll(x) ntohll(x)
#endif

extern char g_namespace[];
extern int gettimeofday(struct timeval *tp, struct timezone *tzp);
long db_time_now(int i);
void db_set_error(char *fnc, char *msg, char *diag_msg);
/**
 * @brief
 *  This structure is used to represent the cursor state for a multirow query
 *  result. The row field keep track of which row is the current row (or was
 *  last returned to the caller). The count field contains the total number of
 *  rows that are available in the resultset.
 *
 */
struct db_query_state {
	int row;
	int count;
	query_cb_t query_cb;
};
typedef struct db_query_state db_query_state_t;

#define FIND_JOBS_BY_QUE 1

/**
 * @brief
 * Each database object type supports most of the following 6 operations:
 *	- insertion
 *	- updation
 *	- deletion
 *	- loading
 *	- find rows matching a criteria
 *	- get next row from a cursor (created in a find command)
 *
 * The following structure has function pointers to all the above described
 * operations.
 *
 */
struct pbs_db_fn {
	int (*pbs_db_save_obj) (void *conn, pbs_db_obj_info_t *obj, int savetype);
	int (*pbs_db_delete_obj) (void *conn, pbs_db_obj_info_t *obj);
	int (*pbs_db_load_obj) (void *conn, pbs_db_obj_info_t *obj);
	int (*pbs_db_find_obj) (void *conn, void *state, pbs_db_obj_info_t *obj, pbs_db_query_options_t *opts);
	int (*pbs_db_del_attr_obj)(void *conn, void *obj_id, pbs_db_attr_list_t *attr_list);
	void (*pbs_db_reset_obj)(pbs_db_obj_info_t *obj);
};

typedef struct pbs_db_fn pbs_db_fn_t;

unsigned long long pbs_ntohll(unsigned long long);
int convert_db_attr_list_to_asmap(as_hashmap *attrlist, pbs_db_attr_list_t *attr_list);
int convert_asmap_to_db_attr_list(as_hashmap *attrmap, pbs_db_attr_list_t *attr_list);


/* job functions */
int pbs_db_save_job(void *conn, pbs_db_obj_info_t *obj, int savetype);
int pbs_db_load_job(void *conn, pbs_db_obj_info_t *obj);
int pbs_db_find_job(void *conn, void *st, pbs_db_obj_info_t *obj, pbs_db_query_options_t *opts);
int pbs_db_delete_job(void *conn, pbs_db_obj_info_t *obj);

int pbs_db_save_jobscr(void *conn, pbs_db_obj_info_t *obj, int savetype);
int pbs_db_load_jobscr(void *conn, pbs_db_obj_info_t *obj);

/* resv functions */
int pbs_db_save_resv(void *conn, pbs_db_obj_info_t *obj, int savetype);
int pbs_db_load_resv(void *conn, pbs_db_obj_info_t *obj);
int pbs_db_find_resv(void *conn, void *st, pbs_db_obj_info_t *obj, pbs_db_query_options_t *opts);
int pbs_db_delete_resv(void *conn, pbs_db_obj_info_t *obj);

/* svr functions */
int pbs_db_save_svr(void *conn, pbs_db_obj_info_t *obj, int savetype);
int pbs_db_load_svr(void *conn, pbs_db_obj_info_t *obj);

/* node functions */
int pbs_db_save_node(void *conn, pbs_db_obj_info_t *obj, int savetype);
int pbs_db_load_node(void *conn, pbs_db_obj_info_t *obj);
int pbs_db_find_node(void *conn, void *st, pbs_db_obj_info_t *obj, pbs_db_query_options_t *opts);
int pbs_db_delete_node(void *conn, pbs_db_obj_info_t *obj);

/* mominfo_time functions */
int pbs_db_save_mominfo_tm(void *conn, pbs_db_obj_info_t *obj, int savetype);
int pbs_db_load_mominfo_tm(void *conn, pbs_db_obj_info_t *obj);

/* queue functions */
int pbs_db_save_que(void *conn, pbs_db_obj_info_t *obj, int savetype);
int pbs_db_load_que(void *conn, pbs_db_obj_info_t *obj);
int pbs_db_find_que(void *conn, void *st, pbs_db_obj_info_t *obj, pbs_db_query_options_t *opts);
int pbs_db_delete_que(void *conn, pbs_db_obj_info_t *obj);

/* scheduler functions */
int pbs_db_save_sched(void *conn, pbs_db_obj_info_t *obj, int savetype);
int pbs_db_load_sched(void *conn, pbs_db_obj_info_t *obj);

int pbs_db_find_sched(void *conn, void *st, pbs_db_obj_info_t *obj, pbs_db_query_options_t *opts);
int pbs_db_delete_sched(void *conn, pbs_db_obj_info_t *obj);

int pbs_db_del_attr_job(void *conn, void *obj_id, pbs_db_attr_list_t *attr_list);
int pbs_db_del_attr_sched(void *conn, void *obj_id, pbs_db_attr_list_t *attr_list);
int pbs_db_del_attr_resv(void *conn, void *obj_id, pbs_db_attr_list_t *attr_list);
int pbs_db_del_attr_svr(void *conn, void *obj_id, pbs_db_attr_list_t *attr_list);
int pbs_db_del_attr_que(void *conn, void *obj_id, pbs_db_attr_list_t *attr_list);
int pbs_db_del_attr_node(void *conn, void *obj_id, pbs_db_attr_list_t *attr_list);

int pbs_db_add_update_attr_node(void *conn, pbs_db_obj_info_t *obj, void *obj_id, pbs_db_attr_list_t *attr_list);

void pbs_db_reset_job(pbs_db_obj_info_t *obj);
void pbs_db_reset_svr(pbs_db_obj_info_t *obj);
void pbs_db_reset_que(pbs_db_obj_info_t *obj);
void pbs_db_reset_resv(pbs_db_obj_info_t *obj);
void pbs_db_reset_sched(pbs_db_obj_info_t *obj);
void pbs_db_reset_mominfo(pbs_db_obj_info_t *obj);



#ifdef	__cplusplus
}
#endif

#endif	/* _DB_AEROSPIKE_H */

