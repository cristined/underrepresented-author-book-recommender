import pandas as pd
import numpy as np
import os
import load_data
import get_user
from collections import Counter
from gd_new_user import GD
import operator


class UserRecs(object):
    def __init__(self):
        """
        Initiate user recommendation class
        """
        self._get_books_data()
        self.k_names = {0: 'Graphic Novels',
                        1: 'Required Reading',
                        2: 'Young Adult',
                        3: 'Mind Openers',
                        4: 'Murder',
                        5: 'Suspense',
                        6: 'Classics',
                        7: 'Love',
                        8: 'Childrens',
                        9: 'Vampires',
                        10: 'Series',
                        11: 'Collections',
                        12: 'Horror'}

    def fit(self, user_id, api_key, rank=41, negative=False):
        """
        Fit the user recommendations based on their goodreads ID
        Default rank to best rank determined in testing rank=41
        Default gd fit method to best determined in testing negative=False
        """
        self.rank = rank
        self.user_id = user_id
        self.negative = negative
        self._get_items_matrix()
        self.get_user_data(user_id, api_key)
        self.get_gd_user()
        self.get_recommendations()

    def _get_books_data(self):
        """
        Load book data from postgres database
        """
        self.df_books = load_data.get_books()
        self.df_authors = load_data.get_classified_authors()
        self.df_authors_books = load_data.get_books_to_authors()
        self.df_isbn_best_book_id = load_data.get_isbn_to_best_book_id()
        df_books_classified = load_data.merge_to_classify_books()
        df_books_classified['authorbook_id'] = df_books_classified['best_book_id'].map(str) + ' ' + df_books_classified['author_id'].map(str)
        self.df_books_classified = df_books_classified
        df_ab_classified = df_books_classified.groupby(['race','gender'])['authorbook_id'].nunique().reset_index()
        df_ab_classified['percentage'] = df_ab_classified['authorbook_id'] / df_ab_classified['authorbook_id'].sum()
        df_ab_classified['race_gender'] = df_ab_classified['race'] + ' ' + df_ab_classified['gender']
        self.df_ab_classified = df_ab_classified

    def _get_items_matrix(self):
        """
        Get book matrix with the correct rank created by spark
        """
        item_test_npy = '../data/k-matrix/{}_test_item_matrix.npy'.format(self.rank)
        self.items_matrix = np.load(item_test_npy)
        self.items_matrix_books = self.items_matrix[::, 0]
        self.items_matrix_factors = self.items_matrix[::, 1]

    def get_user_data(self, user_id, api_key):
        """
        Get user ratings from goodreads
        Create user most common categories
        Create user vs goodreads matrix to be used in boosting later
        """
        self.df_user_ratings, self.books_read_10k, self.books_read = get_user.get_user_read_books(user_id, api_key, self.df_isbn_best_book_id, self.df_books)
        user_k = pd.merge(self.df_user_ratings,
                          self.df_books_classified[['best_book_id', 'k_label']],
                          how='left', left_on='book_id', right_on='best_book_id')
        self.count_ks = Counter(user_k['k_label'])
        most_common_ks = dict()
        for k, count in Counter(self.df_books['k_label']).most_common(13):
            most_common_ks[k] = self.count_ks[k]/float(count)
        most_common_ks = sorted(most_common_ks.items(), key=operator.itemgetter(1))[::-1]
        self.most_common_ks = [k_label for k_label, count in most_common_ks[:5]]
        df_user_ab_classified = get_user.create_user_authorbook_classified(self.df_isbn_best_book_id,
                                                                           self.df_user_ratings,
                                                                           self.df_books_classified)
        df_user_ab_classified['race_gender'] = df_user_ab_classified['race'].map(str) + ' ' + df_user_ab_classified['gender'].map(str)
        self.df_user_ab_classified = df_user_ab_classified
        df_user_v_goodreads = pd.merge(self.df_ab_classified, df_user_ab_classified, left_on='race_gender', right_on='race_gender', how='left')
        df_user_v_goodreads = df_user_v_goodreads[['race_gender','race_x','gender_x','authorbook_id_x','percentage_x','authorbook_id_y','percentage_y']]
        df_user_v_goodreads.columns = ['race_gender', 'race', 'gender', 'gr_count',
                                       'gr_percentage', 'user_count',
                                       'user_percentage']
        df_user_v_goodreads['gr_count'] = df_user_v_goodreads['gr_count'] + 1000
        df_user_v_goodreads['gr_percentage'] = df_user_v_goodreads['gr_count'] / df_user_v_goodreads['gr_count'].sum()
        df_user_v_goodreads['user_count'].fillna(0, inplace=True)
        df_user_v_goodreads['user_percentage'].fillna(0.00001, inplace=True)
        df_user_v_goodreads['user_gr_perc'] = df_user_v_goodreads['user_percentage'] / df_user_v_goodreads['gr_percentage']
        df_user_v_goodreads['user_gr_perc_norm'] = 1 / (1 + df_user_v_goodreads['user_gr_perc'])
        self.df_user_v_goodreads = df_user_v_goodreads

    def plot_user_data(self):
        """
        Plot users goodreads data and list how many books were used in fitting
        the recommendations
        """
        print("{} out of {} books that you have read are in the top 10,000 books on goodreads".format(self.books_read_10k, self.books_read))
        get_user.plot_user_authorbook_classified(self.df_user_ab_classified)

    def get_gd_user(self):
        """
        Get user vector from gradient descent using best number of iterations
        and learning rate
        """
        gd = GD(num_iterations=100, alpha=0.01, negative=self.negative)
        gd.fit(self.df_user_ratings, self.items_matrix)
        self.gd = gd

    def get_recommendations(self):
        """
        Combine user factors and book factors to create recommendations
        Filter on unread books
        Boost ratings based off of the user's reading diversity scores
        """
        book_factors = np.array([factors for factors in self.items_matrix_factors]).T
        recommendations = np.dot(self.gd.user_factors, book_factors)
        book_recs_arr = np.dstack((self.items_matrix_books.reshape((-1)), recommendations.reshape((-1))))[0]
        df_book_rec = pd.DataFrame(book_recs_arr, columns=['best_book_id','rating_guess'])
        df_books_rec_ratings = pd.merge(df_book_rec, self.df_user_ratings[['book_id','rating']], left_on=['best_book_id'], right_on=['book_id'], how='left')
        df_books_unread = df_books_rec_ratings[df_books_rec_ratings.rating.isnull()]
        df_books_unread_classified = pd.merge(df_books_unread, self.df_books_classified, left_on='best_book_id', right_on='best_book_id', how='inner')
        dict_user_goodreads_boost = self.df_user_v_goodreads.set_index('race_gender')['user_gr_perc_norm'].to_dict()
        df_books_unread_classified = df_books_unread_classified[df_books_unread_classified['k_label'] != 8] # Hiding childrens books from results for now
        df_books_unread_classified['race_gender'] = df_books_unread_classified['race'] + ' ' + df_books_unread_classified['gender']
        df_books_unread_classified['rating_guess'] = 5 * df_books_unread_classified['rating_guess'] / df_books_unread_classified['rating_guess'].max()
        df_books_unread_classified['boost'] = df_books_unread_classified['race_gender'].map(lambda x: dict_user_goodreads_boost.get(x, 0))
        df_books_unread_classified['boosted_ratings'] = df_books_unread_classified['boost'] + df_books_unread_classified['rating_guess']
        self.df_recommendations = df_books_unread_classified.sort_values('boosted_ratings', ascending=False)
        self.get_final_rec_df()

    def get_final_rec_df(self):
        """
        Removes duplicate books (multiple authors) and returns data frame with
        book list and includes images if you would like to display them
        """
        rec_ind = self.df_recommendations[['best_book_id']].reset_index(drop=True).reset_index()
        rec_ind = rec_ind.groupby(['best_book_id']).min().reset_index()
        rec_ind = pd.merge(rec_ind, self.df_books, how='left',
                           left_on='best_book_id', right_on='best_book_id'
                           ).sort_values('index')
        self.book_recs = rec_ind
        return self.book_recs

    def print_categorical_recs(self, n):
        """
        Print recs by top 5 k means clusers for this user and the top books not
        in their top 5 k's
        """
        print('==='*20)
        for k in self.most_common_ks:
            print(self.k_names[k])
            print(list(self.book_recs[self.book_recs['k_label'] == k]['title'])[:n])
            print('==='*20)
        print("Top Recommendations not in Your Top Categories")
        print(list(self.book_recs[self.book_recs['k_label'].isin(self.most_common_ks) == False]['title'])[:n])
        print('==='*20)


def pretty_print(df, length):
    df = df[['title', 'race_gender', 'k_label']].head(length)
    print(str(df))


if __name__ == '__main__':

    api_key = os.environ['GOODREADS_API_KEY']

    Catherine = 53106890
    Cristine = 2624891
    Tomas = 5877959
    Moses = 8683925
    Rohit = 76691842

    recs = UserRecs()
    recs.fit(Cristine, api_key)
    print("Recommendations for {}".format('Cristine'))
    print(pretty_print(Cristine_Recs.df_recommendations, 10))
    recs.print_categorical_recs(10)
    # recs.plot_user_data()
